import math

import pytest

from app.privacy import sanitize_auto_capture_events


def test_sanitize_auto_capture_events_is_targeted_and_does_not_mutate_input():
    body = {
        "batch_id": "batch-1",
        "events": [
            {
                "event": "$click",
                "properties": {
                    "text": "click-secret",
                    "tag": "input",
                    "href": "https://example.test/reset?token=secret",
                    "x": 10,
                    "y": 20,
                },
                "context": {
                    "browser": {"name": "Firefox", "version": "128"},
                    "referrer": "https://example.test/reset?token=referrer-secret",
                    "page": {
                        "url": "https://example.test/account?token=url-secret",
                        "title": "Password for title-secret",
                        "path": "/account/path-secret",
                        "search": "?token=search-secret",
                    },
                    "locale": "en-CA",
                },
            },
            {
                "event": "$rage_click",
                "properties": {
                    "text": "rage-secret",
                    "clickCount": 3,
                    "classes": "secret-class",
                    "x": 10,
                    "y": 20,
                },
            },
            {
                "event": "custom_click",
                "properties": {"text": "allowed-custom-text"},
            },
            {
                "event": "$click",
                "properties": "invalid-but-untouched",
            },
        ],
    }

    sanitized = sanitize_auto_capture_events(body)

    assert sanitized == {
        "batch_id": "batch-1",
        "events": [
            {
                "event": "$click",
                "properties": {"tag": "input", "x": 10, "y": 20},
                "context": {
                    "browser": {"name": "Firefox", "version": "128"},
                    "locale": "en-CA",
                },
            },
            {
                "event": "$rage_click",
                "properties": {"clickCount": 3, "x": 10, "y": 20},
            },
            {
                "event": "custom_click",
                "properties": {"text": "allowed-custom-text"},
            },
            {
                "event": "$click",
                "properties": {},
            },
        ],
    }
    assert body["events"][0]["properties"]["text"] == "click-secret"
    assert body["events"][1]["properties"]["text"] == "rage-secret"
    assert "page" in body["events"][0]["context"]
    assert "referrer" in body["events"][0]["context"]


def test_sanitize_auto_capture_events_returns_safe_payload_unchanged():
    body = {
        "events": [
            {
                "event": "$click",
                "properties": {"tag": "button", "x": 10, "y": 20},
            }
        ]
    }

    assert sanitize_auto_capture_events(body) is body


def test_sanitize_auto_capture_events_leaves_invalid_event_names_for_validation():
    body = {"events": [{"event": [], "properties": {"text": "invalid"}}]}

    assert sanitize_auto_capture_events(body) is body


@pytest.mark.parametrize(
    ("event_name", "properties", "expected"),
    [
        (
            "$click",
            {"tag": "custom-element", "x": 0, "y": 100_000.0},
            {"tag": "custom-element", "x": 0, "y": 100_000.0},
        ),
        (
            "$rage_click",
            {"tag": "button", "clickCount": 100, "x": 10.5, "y": 20},
            {"tag": "button", "clickCount": 100, "x": 10.5, "y": 20},
        ),
        (
            "$click",
            {
                "tag": "INPUT-password-secret",
                "x": True,
                "y": math.inf,
            },
            {},
        ),
        (
            "$click",
            {
                "tag": "a" * 65,
                "x": -(10**1000),
                "y": 10**1000,
            },
            {},
        ),
        (
            "$rage_click",
            {
                "tag": "button",
                "clickCount": 3.0,
                "x": "coordinate-secret",
                "y": False,
            },
            {"tag": "button"},
        ),
        (
            "$rage_click",
            {"clickCount": 2},
            {},
        ),
        (
            "$rage_click",
            {"clickCount": 101},
            {},
        ),
    ],
)
def test_sanitize_auto_capture_events_enforces_canonical_property_values(
    event_name,
    properties,
    expected,
):
    body = {"events": [{"event": event_name, "properties": properties}]}

    sanitized = sanitize_auto_capture_events(body)

    assert sanitized["events"][0]["properties"] == expected


def test_sanitize_auto_capture_events_removes_click_page_context_only():
    body = {
        "events": [
            {
                "event": "$click",
                "properties": {"tag": "a", "x": 1, "y": 2},
                "context": {
                    "page": {
                        "url": "https://example.test/?token=url-secret",
                        "title": "title-secret",
                        "path": "/path-secret",
                    },
                    "referrer": "https://referrer.test/?token=referrer-secret",
                    "browser": {"name": "Firefox", "version": "128"},
                    "locale": "en-CA",
                },
            },
            {
                "event": "custom_click",
                "properties": {"page": "custom-page"},
                "context": {
                    "page": {"url": "https://custom.test/"},
                    "referrer": "https://custom-referrer.test/",
                },
            },
        ]
    }

    sanitized = sanitize_auto_capture_events(body)

    assert sanitized["events"][0]["context"] == {
        "browser": {"name": "Firefox", "version": "128"},
        "locale": "en-CA",
    }
    assert sanitized["events"][1] is body["events"][1]
    assert sanitized["events"][1]["context"] == body["events"][1]["context"]
