"""Recursive JSON and serialized event bounds enforced before queue ownership."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from apdl.types import (
    MAX_EVENT_SERIALIZED_BYTES,
    MAX_JSON_CONTAINER_ENTRIES,
    MAX_JSON_DEPTH,
    MAX_JSON_TOTAL_NODES,
    MAX_PROPERTY_KEY_LENGTH,
    MAX_STRING_PROPERTY_LENGTH,
    canonicalize_event_payload,
    serialized_json_size,
)


def event(properties: object) -> dict:
    return {
        "event": "checkout",
        "type": "track",
        "anonymous_id": "anon_1",
        "properties": properties,
        "context": {},
        "timestamp": "2026-07-13T12:00:00Z",
        "message_id": "msg_1",
    }


def with_context(context: object) -> dict:
    payload = event({"valid": True})
    payload["context"] = context
    return payload


def test_canonical_json_is_detached_and_serializable():
    properties = {"items": [{"sku": "one", "price": 12.5}], "paid": True}

    canonical = canonicalize_event_payload(event(properties))
    properties["paid"] = False

    assert canonical["properties"]["paid"] is True
    assert serialized_json_size(canonical) < MAX_EVENT_SERIALIZED_BYTES


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(ValueError, match="non-finite"):
        canonicalize_event_payload(event({"value": value}))


@pytest.mark.parametrize(
    "value",
    [
        ("tuple",),
        {"set"},
        b"bytes",
        datetime(2026, 7, 13, tzinfo=timezone.utc),
    ],
)
def test_non_json_python_values_are_rejected(value):
    with pytest.raises(ValueError, match="unsupported non-JSON"):
        canonicalize_event_payload(event({"value": value}))


def test_cyclic_objects_are_rejected():
    cycle: dict = {}
    cycle["self"] = cycle

    with pytest.raises(ValueError, match="cyclic JSON"):
        canonicalize_event_payload(event(cycle))


def test_reused_non_cyclic_container_is_allowed_and_detached():
    shared = {"sku": "one"}

    canonical = canonicalize_event_payload(event({"first": shared, "second": shared}))

    assert canonical["properties"]["first"] == shared
    assert canonical["properties"]["second"] == shared
    assert canonical["properties"]["first"] is not canonical["properties"]["second"]


def test_nesting_depth_is_bounded():
    value: object = "leaf"
    # Event root is depth zero and properties is depth one. This creates one
    # level past the canonical whole-event depth limit.
    for _ in range(MAX_JSON_DEPTH):
        value = [value]

    with pytest.raises(ValueError, match="maximum depth"):
        canonicalize_event_payload(event({"nested": value}))


@pytest.mark.parametrize(
    "value",
    [
        list(range(MAX_JSON_CONTAINER_ENTRIES + 1)),
        {f"key_{index}": index for index in range(MAX_JSON_CONTAINER_ENTRIES + 1)},
    ],
)
def test_container_cardinality_is_bounded(value):
    with pytest.raises(ValueError, match="maximum cardinality"):
        canonicalize_event_payload(event({"nested": value}))


def test_total_node_count_is_bounded():
    # Each child list is individually legal; their combined nodes exceed the
    # per-event total once the canonical envelope is included.
    value = {
        f"group_{index}": list(range(MAX_JSON_CONTAINER_ENTRIES))
        for index in range(10)
    }

    with pytest.raises(ValueError, match=f"node count of {MAX_JSON_TOTAL_NODES}"):
        canonicalize_event_payload(event(value))


def test_top_level_property_key_and_string_lengths_match_ingestion():
    with pytest.raises(ValueError, match="property key"):
        canonicalize_event_payload(event({"x" * (MAX_PROPERTY_KEY_LENGTH + 1): 1}))
    with pytest.raises(ValueError, match="string property"):
        canonicalize_event_payload(
            event({"value": "x" * (MAX_STRING_PROPERTY_LENGTH + 1)})
        )

    # Nested values are bounded by depth/cardinality/nodes/bytes, not by the
    # shallow property limits, matching the ingestion model.
    canonicalize_event_payload(
        event({"nested": {"value": "x" * (MAX_STRING_PROPERTY_LENGTH + 1)}})
    )


def test_serialized_event_size_is_bounded():
    properties = {
        f"part_{index}": "x" * MAX_STRING_PROPERTY_LENGTH for index in range(9)
    }

    with pytest.raises(ValueError, match="maximum serialized size"):
        canonicalize_event_payload(event(properties))


def test_complete_canonical_context_is_preserved():
    context = {
        "library": {"name": "apdl-python", "version": "0.1.0"},
        "browser": {"name": "Firefox", "version": "128"},
        "os": {"name": "Linux", "version": "6.8"},
        "device": {"type": "desktop"},
        "screen": {"width": 1_920, "height": 1_080},
        "viewport": {"width": 1_280, "height": 720},
        "page": {
            "url": "https://example.test/checkout?q=1",
            "title": "Checkout",
            "path": "/checkout",
            "search": "?q=1",
        },
        "locale": "en-CA",
        "timezone": "America/Toronto",
        "referrer": "https://example.test/cart",
    }

    canonical = canonicalize_event_payload(with_context(context))

    assert canonical["context"] == context


@pytest.mark.parametrize(
    "context",
    [
        {"geo": {"country": "CA"}},
        {"browser": {"name": "Firefox", "version": "128", "browserName": "x"}},
        {
            "page": {
                "url": "https://example.test",
                "title": "Home",
                "path": "/",
                "search": "",
                "referrer": "https://referrer.test",
            }
        },
    ],
)
def test_context_rejects_unknown_fields_and_aliases(context):
    with pytest.raises(ValueError, match="invalid event payload"):
        canonicalize_event_payload(with_context(context))


@pytest.mark.parametrize(
    "context",
    [
        {"library": {"name": "apdl-python"}},
        {
            "page": {
                "url": "https://example.test",
                "title": "Home",
                "path": "/",
            }
        },
    ],
)
def test_context_rejects_missing_nested_fields(context):
    with pytest.raises(ValueError, match="invalid event payload"):
        canonicalize_event_payload(with_context(context))


@pytest.mark.parametrize(
    "context",
    [
        [],
        {"library": {"name": "", "version": "0.1.0"}},
        {"device": {"type": "x" * 65}},
        {"screen": {"width": True, "height": 1_080}},
        {"viewport": {"width": 100_001, "height": 720}},
        {
            "page": {
                "url": "x" * 4_097,
                "title": "Home",
                "path": "/",
                "search": "",
            }
        },
        {"locale": "x" * 129},
        {"timezone": "x" * 129},
        {"referrer": "x" * 4_097},
    ],
)
def test_context_rejects_malformed_values_and_limit_violations(context):
    with pytest.raises(ValueError, match="invalid event payload"):
        canonicalize_event_payload(with_context(context))


@pytest.mark.parametrize(
    "context",
    [
        {"browser": None},
        {"page": None},
        {"locale": None},
    ],
)
def test_context_rejects_explicit_null_instead_of_treating_it_as_omitted(context):
    with pytest.raises(ValueError, match="omitted rather than null"):
        canonicalize_event_payload(with_context(context))


@pytest.mark.parametrize(
    "field",
    [
        "user_id",
        "anonymous_id",
        "group_id",
        "properties",
        "traits",
        "session_id",
    ],
)
def test_event_rejects_explicit_null_optional_fields(field):
    payload = event({"valid": True})
    if field == "anonymous_id":
        payload["user_id"] = "user_1"
    payload[field] = None

    with pytest.raises(ValueError, match="omitted rather than null"):
        canonicalize_event_payload(payload)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-13T12:00:00Z",
        "2026-07-13T12:00:00.1Z",
        "2026-07-13T12:00:00.123Z",
        "2026-07-13T12:00:00.123456Z",
    ],
)
def test_canonical_rfc3339_timestamp_precision_is_accepted(timestamp):
    payload = event({"valid": True})
    payload["timestamp"] = timestamp

    assert canonicalize_event_payload(payload)["timestamp"] == timestamp


@pytest.mark.parametrize(
    "timestamp",
    [
        "not-a-date",
        "0999-07-13T12:00:00Z",
        "2026-07-13 12:00:00Z",
        "2026-07-13T12:00:00z",
        "2026-07-13T12:00:00+00:00",
        "2026-07-13T12:00:00.1234567Z",
        "2026-02-30T12:00:00Z",
        "2026-07-13T24:00:00Z",
        "2026-07-13T12:00:60Z",
    ],
)
def test_invalid_timestamps_are_rejected_before_enqueue(timestamp):
    payload = event({"valid": True})
    payload["timestamp"] = timestamp

    with pytest.raises(ValueError, match="timestamp"):
        canonicalize_event_payload(payload)


def test_invalid_unicode_is_rejected_before_enqueue():
    with pytest.raises(ValueError, match="canonical JSON"):
        canonicalize_event_payload(event({"value": "\ud800"}))
