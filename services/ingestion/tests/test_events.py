"""Port of C++ test_events_handler.cpp to Python/pytest.

Tests the POST /v1/events endpoint via httpx AsyncClient against the FastAPI
app with a mock Redis backend.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import CredentialKind, Principal, authenticate_request
from app.main import app
from app.middleware.rate_limit import (
    BROWSER_BYTE_LIMIT,
    BROWSER_EVENT_LIMIT,
    PROJECT_BYTE_LIMIT,
    PROJECT_EVENT_LIMIT,
    BucketDebit,
    BucketLimit,
    _HIERARCHICAL_TOKEN_BUCKET_LUA,
    _admit,
)
from app.routers import events as events_router
from app.streaming.redis_producer import (
    EVENT_STREAM_MAX_ENTRIES,
    _BOUNDED_XADD_LUA,
)
from app.validation.json_contract import MAX_REQUEST_BYTES

API_KEY = "proj_testproj_abcdefghijklmnop"
HEADERS = {"X-API-Key": API_KEY}
URL = "/v1/events"


def canonical_event(
    event: str = "test_event",
    event_type: str = "track",
    **overrides,
):
    value = {
        "event": event,
        "type": event_type,
        "anonymous_id": "anon-test",
        "timestamp": "2026-07-13T12:00:00.000Z",
        "context": {},
        "message_id": "message-test",
    }
    value.update(overrides)
    return value


def publisher_calls():
    return [
        call
        for call in app.state.redis.eval.await_args_list
        if call.args[0] == _BOUNDED_XADD_LUA
    ]


def quota_calls(stage: str | None = None):
    calls = [
        call
        for call in app.state.redis.eval.await_args_list
        if call.args[0] == _HIERARCHICAL_TOKEN_BUCKET_LUA
    ]
    if stage is None:
        return calls
    return [
        call
        for call in calls
        if any(
            key.startswith(f"apdl:rate:{stage}:")
            for key in quota_keys(call)
        )
    ]


def quota_keys(call) -> tuple[str, ...]:
    key_count = int(call.args[1])
    return call.args[2 : 2 + key_count]


def quota_debits(call) -> list[tuple[str, tuple[int, ...]]]:
    keys = quota_keys(call)
    arguments = call.args[2 + len(keys) :]
    return [
        (key, arguments[index * 4 : (index + 1) * 4])
        for index, key in enumerate(keys)
    ]


class StatefulQuotaEvaluator:
    """Stateful reference double for the Lua check-all/debit-all contract."""

    def __init__(self) -> None:
        self.tokens: dict[str, int] = {}

    async def evaluate(self, script, key_count, *args):
        assert script == _HIERARCHICAL_TOKEN_BUCKET_LUA
        keys = args[:key_count]
        raw_limits = args[key_count:]
        candidates = []
        for index, key in enumerate(keys):
            capacity, _refill, cost, _ttl = raw_limits[
                index * 4 : (index + 1) * 4
            ]
            tokens = self.tokens.get(key, capacity)
            candidates.append((key, capacity, cost, tokens))

        rejected = next(
            (candidate for candidate in candidates if candidate[3] < candidate[2]),
            None,
        )
        if rejected is not None:
            _key, capacity, _cost, tokens = rejected
            return [0, capacity, tokens, 1]

        for key, _capacity, cost, tokens in candidates:
            self.tokens[key] = tokens - cost
        minimum = min(self.tokens[key] for key in keys)
        return [1, 0, minimum, 0]


def published_events() -> list[dict]:
    calls = publisher_calls()
    assert len(calls) == 1
    return [json.loads(value) for value in calls[0].args[5:]]


@pytest.fixture(autouse=True)
def _setup_mock_redis():
    """Inject a mock Redis into app.state before each test."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    async def evaluate(script, _numkeys, *args):
        if script == _BOUNDED_XADD_LUA:
            count = int(args[1])
            return [
                1,
                count,
                *(f"1234567890-{index}".encode() for index in range(count)),
            ]
        if script == _HIERARCHICAL_TOKEN_BUCKET_LUA:
            return [1, 999, 999, 0]
        raise AssertionError("Unexpected Redis script")

    mock_redis.eval = AsyncMock(side_effect=evaluate)
    pipeline = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=pipeline)
    app.state.redis = mock_redis
    app.state.trusted_proxy_networks = ()
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---- Ported from EventHandlerTest in test_events_handler.cpp ----


@pytest.mark.asyncio
async def test_valid_batch_with_track_event(client):
    """ValidBatchWithTrackEvent"""
    payload = {"events": [canonical_event(
        "button_click",
        user_id="usr_123",
        properties={"button": "signup"},
        timestamp="2025-01-01T00:00:00.000Z",
    )]}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 1


@pytest.mark.asyncio
async def test_sdk_aligned_payload_is_published_to_project_stream(client):
    """SDK contract: /v1/events, X-API-Key, events[], and snake_case IDs."""
    payload = {
        "events": [{
            "event": "sdk_aligned_probe",
            "type": "track",
            "anonymous_id": "anon-sdk-1",
            "session_id": "sess-sdk-1",
            "message_id": "msg-sdk-1",
            "timestamp": "2026-05-26T02:26:53.455Z",
            "properties": {"source": "sdk-contract-test"},
            "context": {
                "browser": {"name": "Chrome", "version": "123"},
                "device": {"type": "desktop"},
            },
        }],
    }

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1
    calls = publisher_calls()
    assert len(calls) == 1
    args = calls[0].args
    assert args[2] == "events:raw:testproj"
    assert args[3] == 1
    assert args[4] == EVENT_STREAM_MAX_ENTRIES
    assert "MAXLEN" not in args[0]

    published = published_events()[0]
    assert published["event"] == "sdk_aligned_probe"
    assert published["type"] == "track"
    assert published["anonymous_id"] == "anon-sdk-1"
    assert published["session_id"] == "sess-sdk-1"
    assert published["message_id"] == "msg-sdk-1"
    assert published["properties"] == {"source": "sdk-contract-test"}
    assert published["context"]["browser"] == {"name": "Chrome", "version": "123"}
    assert published["context"]["device"] == {"type": "desktop"}
    assert published["project_id"] == "testproj"
    assert "server_timestamp" in published
    assert "ip" in published
    assert "anonymousId" not in published
    assert "sessionId" not in published
    assert "messageId" not in published


@pytest.mark.asyncio
async def test_untrusted_forwarding_headers_do_not_reach_persisted_identity(client):
    resp = await client.post(
        URL,
        json={"events": [canonical_event("spoof-attempt")]},
        headers={
            **HEADERS,
            "X-Forwarded-For": "203.0.113.99",
            "X-Real-IP": "203.0.113.98",
        },
    )

    assert resp.status_code == 202
    published = published_events()[0]
    assert published["ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_click_auto_capture_is_sanitized_before_redis_publish(client):
    legacy_sentinel = "live-password-value"
    # Longer than the normal property limit, proving text is stripped before
    # validation rather than rejected into an old SDK's retry/offline queue.
    over_limit_sentinel = "live-password-value-" * 500
    payload = {
        "events": [
            {
                "event": "$click",
                "type": "track",
                "anonymous_id": "anon-sdk-1",
                "properties": {
                    "text": legacy_sentinel,
                    "tag": "input",
                    "id": "password",
                    "classes": "account-password",
                    "x": 12,
                    "y": 34,
                },
                "context": {
                    "page": {
                        "url": "https://example.test/reset?token=url-secret",
                        "title": "Reset title-secret",
                        "path": "/reset/path-secret",
                        "search": "?token=search-secret",
                    },
                    "referrer": "https://referrer.test/?token=referrer-secret",
                    "browser": {"name": "Firefox", "version": "128"},
                },
            },
            {
                "event": "$rage_click",
                "type": "track",
                "anonymous_id": "anon-sdk-1",
                "properties": {
                    "text": over_limit_sentinel,
                    "tag": "input",
                    "id": "password",
                    "classes": "account-password",
                    "clickCount": 3,
                    "x": 12,
                    "y": 34,
                },
                "context": {
                    "page": {
                        "url": "https://example.test/rage?token=rage-url-secret",
                        "title": "Rage title-secret",
                        "path": "/rage/path-secret",
                    },
                    "referrer": "https://referrer.test/rage-secret",
                        "device": {"type": "desktop"},
                },
            },
            {
                "event": "$click",
                "type": "track",
                "anonymous_id": "anon-sdk-1",
                "properties": {
                    "tag": "INPUT-password-secret",
                    "x": "x-coordinate-secret",
                    "y": True,
                },
                "context": {
                    "page": {
                        "url": "https://example.test/?token=malformed-url-secret",
                        "title": "Malformed title-secret",
                        "path": "/malformed/path-secret",
                    },
                    "referrer": "https://referrer.test/malformed-secret",
                    "locale": "en-CA",
                },
            },
            {
                "event": "custom_event",
                "type": "track",
                "anonymous_id": "anon-sdk-1",
                "properties": {"text": "ordinary event text", "source": "test"},
            },
        ]
    }
    for index, event in enumerate(payload["events"]):
        event.setdefault("timestamp", "2026-07-13T12:00:00.000Z")
        event.setdefault("context", {})
        event.setdefault("message_id", f"message-auto-{index}")
        event.setdefault("session_id", "session-auto")

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 202
    assert resp.json() == {"accepted": 4}
    published = published_events()
    assert len(published) == 4
    assert published[0]["properties"] == {"tag": "input", "x": 12, "y": 34}
    assert published[0]["context"] == {
        "browser": {"name": "Firefox", "version": "128"}
    }
    assert published[1]["properties"] == {
        "tag": "input",
        "clickCount": 3,
        "x": 12,
        "y": 34,
    }
    assert published[1]["context"] == {"device": {"type": "desktop"}}
    assert published[2]["properties"] == {}
    assert published[2]["context"] == {"locale": "en-CA"}
    assert published[3]["properties"] == {
        "text": "ordinary event text",
        "source": "test",
    }
    serialized = json.dumps(published)
    assert legacy_sentinel not in serialized
    assert over_limit_sentinel not in serialized
    for sentinel in (
        "url-secret",
        "title-secret",
        "path-secret",
        "referrer-secret",
        "coordinate-secret",
        "INPUT-password-secret",
        "rage-secret",
    ):
        assert sentinel not in serialized


@pytest.mark.asyncio
async def test_feature_flag_exposure_payload_is_published(client):
    payload = {
        "events": [{
            "event": "$feature_flag_exposure",
            "type": "track",
            "anonymous_id": "anon-sdk-1",
            "session_id": "sess-sdk-1",
            "message_id": "msg-sdk-1",
            "timestamp": "2026-05-26T02:26:53.455Z",
            "context": {},
            "properties": {
                "flag_key": "checkout-gate",
                "variant": "treatment",
                "reason": "fallthrough",
                "rule_id": None,
                "rollout_bucket": 7.31,
                "variant_bucket": 74.2,
                "rollout_percentage": 100,
                "bucket_by": "user_id",
                "config_version": 3,
                "source": "initial_fetch",
                "page": "/checkout",
                "component": "CheckoutPage",
            },
        }],
    }

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1
    published = published_events()[0]
    assert published["event"] == "$feature_flag_exposure"
    assert published["type"] == "track"
    assert published["properties"] == payload["events"][0]["properties"]


@pytest.mark.asyncio
async def test_feature_flag_exposure_rejects_camel_case_identity(client):
    payload = {
        "events": [{
            "event": "$feature_flag_exposure",
            "type": "track",
            "anonymousId": "anon-sdk-1",
            "session_id": "sess-sdk-1",
            "message_id": "msg-sdk-1",
            "timestamp": "2026-05-26T02:26:53.455Z",
            "properties": {
                "flag_key": "checkout-gate",
                "variant": "treatment",
                "reason": "fallthrough",
                "rule_id": None,
                "rollout_bucket": 7.31,
                "variant_bucket": 74.2,
                "rollout_percentage": 100,
                "bucket_by": "user_id",
                "config_version": 3,
                "source": "initial_fetch",
                "page": "/checkout",
                "component": "CheckoutPage",
            },
        }],
    }

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 400
    body = resp.json()
    fields = {error["field"] for error in body["errors"]}
    assert "events[0].anonymousId" in fields
    assert "events[0].user_id" in fields
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_feature_flag_exposure_rejects_boolean_value_payload(client):
    payload = {
        "events": [{
            "event": "$feature_flag_exposure",
            "type": "track",
            "anonymous_id": "anon-sdk-1",
            "session_id": "sess-sdk-1",
            "message_id": "msg-sdk-1",
            "timestamp": "2026-05-26T02:26:53.455Z",
            "properties": {
                "flag_key": "checkout-gate",
                "value": True,
                "reason": "fallthrough",
                "rule_id": None,
                "rollout_bucket": 7.31,
                "variant_bucket": 74.2,
                "rollout_percentage": 100,
                "bucket_by": "user_id",
                "config_version": 3,
                "source": "initial_fetch",
                "page": "/checkout",
                "component": "CheckoutPage",
            },
        }],
    }

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 400
    body = resp.json()
    fields = {error["field"] for error in body["errors"]}
    assert "events[0].properties.variant" in fields
    assert "events[0].properties.value" in fields
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_valid_batch_with_anonymous_id(client):
    """ValidBatchWithAnonymousId"""
    payload = {"events": [canonical_event(
        "page_view",
        anonymous_id="anon_abc123",
        properties={"url": "/home"},
    )]}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 1


@pytest.mark.asyncio
async def test_reject_batch_with_camel_case_ids(client):
    payload = {
        "events": [{
            "event": "page",
            "type": "page",
            "anonymousId": "anon_abc123",
            "userId": "user_456",
            "timestamp": "2026-07-13T12:00:00.000Z",
            "context": {},
            "message_id": "message-alias",
        }],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    fields = {error["field"] for error in resp.json()["errors"]}
    assert "events[0].anonymousId" in fields
    assert "events[0].userId" in fields


@pytest.mark.asyncio
async def test_reject_missing_events_field(client):
    """RejectMissingEventsField"""
    payload = {"data": []}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"
    fields = [e["field"] for e in body["errors"]]
    assert "events" in fields


@pytest.mark.asyncio
async def test_reject_empty_events_array(client):
    """RejectEmptyEventsArray"""
    payload = {"events": []}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"
    fields = [e["field"] for e in body["errors"]]
    assert "events" in fields


@pytest.mark.asyncio
async def test_reject_event_without_identifier(client):
    """RejectEventWithoutIdentifier"""
    payload = {
        "events": [{
            "event": "test_event",
            "properties": {},
        }],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"
    found_user_id_error = any(
        "user_id" in e["field"] for e in body["errors"]
    )
    assert found_user_id_error


@pytest.mark.asyncio
async def test_reject_event_without_name_or_type(client):
    """RejectEventWithoutNameOrType"""
    payload = {
        "events": [{
            "user_id": "usr_123",
            "properties": {"key": "val"},
        }],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"
    found_event_error = any("event" in e["field"] for e in body["errors"])
    assert found_event_error


@pytest.mark.asyncio
async def test_reject_invalid_event_type(client):
    """RejectInvalidEventType"""
    payload = {
        "events": [{
            "type": "invalid_type",
            "event": "test",
            "user_id": "usr_123",
        }],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_reject_non_object_body(client):
    """RejectNonObjectBody -- sending a JSON array instead of object."""
    resp = await client.post(
        URL,
        content=json.dumps([1, 2, 3]),
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    # The list [1,2,3] is valid JSON but not a dict, so validation fails
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"
    fields = [e["field"] for e in body["errors"]]
    assert "body" in fields


@pytest.mark.asyncio
async def test_reject_duplicate_json_keys_before_enqueue(client):
    raw = (
        '{"events":[{"event":"first","event":"second","type":"track",'
        '"anonymous_id":"anon","timestamp":"2026-07-13T12:00:00.000Z",'
        '"context":{},"message_id":"message-duplicate"}]}'
    )
    resp = await client.post(
        URL,
        content=raw,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Duplicate JSON object key" in resp.json()["message"]
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_reject_nonfinite_json_before_enqueue(client):
    raw = (
        '{"events":[{"event":"test","type":"track",'
        '"anonymous_id":"anon","timestamp":"2026-07-13T12:00:00.000Z",'
        '"context":{},"message_id":"message-nan","properties":{"value":NaN}}]}'
    )
    resp = await client.post(
        URL,
        content=raw,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "Non-finite" in resp.json()["message"]
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_reject_non_utf8_json_string_before_enqueue(client):
    raw = (
        '{"events":[{"event":"test","type":"track",'
        '"anonymous_id":"anon","timestamp":"2026-07-13T12:00:00.000Z",'
        '"context":{},"message_id":"message-surrogate",'
        '"properties":{"value":"\\ud800"}}]}'
    )
    resp = await client.post(
        URL,
        content=raw,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "non-JSON value" in resp.json()["errors"][0]["message"]
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_reject_oversized_request_before_json_parse(client):
    resp = await client.post(
        URL,
        content=b"x" * (MAX_REQUEST_BYTES + 1),
        headers={**HEADERS, "Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.json()["error"] == "payload_too_large"
    assert len(quota_calls("request")) == 1
    assert quota_calls("byte") == []
    assert quota_calls("event") == []
    assert publisher_calls() == []


@pytest.mark.asyncio
async def test_chunked_body_stops_at_bound_before_downstream_stages(monkeypatch):
    messages = iter([
        {
            "type": "http.request",
            "body": b"x" * MAX_REQUEST_BYTES,
            "more_body": True,
        },
        {"type": "http.request", "body": b"y", "more_body": True},
        {
            "type": "http.request",
            "body": b"must-not-be-read",
            "more_body": False,
        },
    ])
    receive_count = 0

    async def receive():
        nonlocal receive_count
        receive_count += 1
        return next(messages)

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": URL,
            "raw_path": URL.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "app": app,
        },
        receive,
    )
    request.state.principal = Principal(
        credential_id="chunked-test",
        project_id="testproj",
        roles=frozenset({"events:write"}),
    )
    request_admission = AsyncMock(return_value=None)
    byte_admission = AsyncMock(return_value=None)
    event_admission = AsyncMock(return_value=None)
    publisher = AsyncMock()
    monkeypatch.setattr(events_router, "admit_request", request_admission)
    monkeypatch.setattr(events_router, "admit_bytes", byte_admission)
    monkeypatch.setattr(events_router, "admit_events", event_admission)
    monkeypatch.setattr(events_router, "publish_batch", publisher)

    resp = await events_router.ingest_events(request)

    assert request.headers.get("content-length") is None
    assert resp.status_code == 413
    assert json.loads(resp.body) == {
        "error": "payload_too_large",
        "message": f"Request body exceeds {MAX_REQUEST_BYTES} bytes",
    }
    assert receive_count == 2
    request_admission.assert_awaited_once()
    byte_admission.assert_not_awaited()
    event_admission.assert_not_awaited()
    publisher.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_request_uses_one_atomic_quota_call_per_stage(client):
    payload = {"events": [canonical_event("quota-test")]}

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 202
    request_calls = quota_calls("request")
    byte_calls = quota_calls("byte")
    event_calls = quota_calls("event")
    assert len(request_calls) == 1
    assert len(byte_calls) == 1
    assert len(event_calls) == 1
    assert len(quota_keys(request_calls[0])) == 4
    assert len(quota_keys(byte_calls[0])) == 4
    assert len(quota_keys(event_calls[0])) == 5

    byte_debits = dict(quota_debits(byte_calls[0]))
    byte_project_key = "apdl:rate:byte:project:testproj"
    byte_credential_key = next(
        key for key in byte_debits if ":credential:" in key
    )
    assert byte_debits[byte_project_key][:2] == (
        PROJECT_BYTE_LIMIT.capacity,
        PROJECT_BYTE_LIMIT.refill_per_second,
    )
    assert byte_debits[byte_credential_key][:2] == (
        BROWSER_BYTE_LIMIT.capacity,
        BROWSER_BYTE_LIMIT.refill_per_second,
    )
    assert BROWSER_BYTE_LIMIT.capacity < PROJECT_BYTE_LIMIT.capacity
    assert (
        BROWSER_BYTE_LIMIT.refill_per_second
        < PROJECT_BYTE_LIMIT.refill_per_second
    )

    event_debits = dict(quota_debits(event_calls[0]))
    project_key = "apdl:rate:event:project:testproj"
    credential_key = next(
        key for key in event_debits if ":credential:" in key
    )
    assert event_debits[project_key][:3] == (
        PROJECT_EVENT_LIMIT.capacity,
        PROJECT_EVENT_LIMIT.refill_per_second,
        1,
    )
    assert event_debits[credential_key][:3] == (
        BROWSER_EVENT_LIMIT.capacity,
        BROWSER_EVENT_LIMIT.refill_per_second,
        1,
    )
    serialized_keys = " ".join(
        (
            *quota_keys(request_calls[0]),
            *byte_debits,
            *event_debits,
        )
    )
    assert API_KEY not in serialized_keys
    assert "test-ingestion" not in serialized_keys
    assert "127.0.0.1" not in serialized_keys
    assert "anon-test" not in serialized_keys


@pytest.mark.asyncio
async def test_malformed_payload_is_charged_by_request_and_byte_before_parsing(
    client,
):
    resp = await client.post(
        URL,
        content=b'{"events": [',
        headers={**HEADERS, "Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert len(quota_calls("request")) == 1
    assert len(quota_calls("byte")) == 1
    assert quota_calls("event") == []
    assert publisher_calls() == []

    byte_debits = quota_debits(quota_calls("byte")[0])
    assert {debit[2] for _key, debit in byte_debits} == {1}


@pytest.mark.asyncio
async def test_maximum_sized_browser_body_fits_subordinate_byte_bucket(client):
    resp = await client.post(
        URL,
        content=b"x" * MAX_REQUEST_BYTES,
        headers={**HEADERS, "Content-Type": "application/json"},
    )

    assert resp.status_code == 400
    assert len(quota_calls("request")) == 1
    assert len(quota_calls("byte")) == 1
    assert quota_calls("event") == []
    assert publisher_calls() == []

    byte_debits = dict(quota_debits(quota_calls("byte")[0]))
    project_key = "apdl:rate:byte:project:testproj"
    credential_key = next(
        key for key in byte_debits if ":credential:" in key
    )
    assert byte_debits[project_key][:3] == (
        PROJECT_BYTE_LIMIT.capacity,
        PROJECT_BYTE_LIMIT.refill_per_second,
        512,
    )
    assert byte_debits[credential_key][:3] == (
        BROWSER_BYTE_LIMIT.capacity,
        BROWSER_BYTE_LIMIT.refill_per_second,
        BROWSER_BYTE_LIMIT.capacity,
    )


@pytest.mark.asyncio
async def test_distinct_credentials_have_distinct_opaque_buckets(client):
    async def authenticate_credential_a(request: Request):
        principal = Principal(
            credential_id="credential-a",
            project_id="testproj",
            roles=frozenset({"events:write"}),
            credential_kind=CredentialKind.BROWSER,
        )
        request.state.principal = principal
        return principal

    async def authenticate_credential_b(request: Request):
        principal = Principal(
            credential_id="credential-b",
            project_id="testproj",
            roles=frozenset({"events:write"}),
            credential_kind=CredentialKind.BROWSER,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_credential_a
    first = await client.post(URL, content=b"{", headers=HEADERS)
    app.dependency_overrides[authenticate_request] = authenticate_credential_b
    second = await client.post(URL, content=b"{", headers=HEADERS)

    assert first.status_code == 400
    assert second.status_code == 400
    request_calls = quota_calls("request")
    credential_keys = [
        next(key for key in quota_keys(call) if ":credential:" in key)
        for call in request_calls
    ]
    assert len(set(credential_keys)) == 2
    assert all("credential-a" not in key for key in credential_keys)
    assert all("credential-b" not in key for key in credential_keys)


@pytest.mark.asyncio
async def test_event_quota_aggregates_repeated_batch_identities(client):
    payload = {
        "events": [
            canonical_event("one", message_id="message-one"),
            canonical_event("two", message_id="message-two"),
            canonical_event(
                "three",
                anonymous_id=None,
                user_id="user-three",
                message_id="message-three",
            ),
        ],
    }
    payload["events"][2].pop("anonymous_id")

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 202
    event_call = quota_calls("event")[0]
    debits = quota_debits(event_call)
    common_debits = [debit for key, debit in debits if ":identity:" not in key]
    identity_debits = [debit for key, debit in debits if ":identity:" in key]
    assert len(common_debits) == 4
    assert {debit[2] for debit in common_debits} == {3}
    assert len(identity_debits) == 2
    assert sorted(debit[2] for debit in identity_debits) == [1, 2]
    assert "anon-test" not in " ".join(quota_keys(event_call))
    assert "user-three" not in " ".join(quota_keys(event_call))


@pytest.mark.asyncio
async def test_same_identity_has_isolated_buckets_across_projects(client):
    async def authenticate_project_a(request: Request):
        principal = Principal(
            credential_id="cross-project-fixture",
            project_id="projecta",
            roles=frozenset({"events:write"}),
            credential_kind=CredentialKind.BROWSER,
        )
        request.state.principal = principal
        return principal

    async def authenticate_project_b(request: Request):
        principal = Principal(
            credential_id="cross-project-fixture",
            project_id="projectb",
            roles=frozenset({"events:write"}),
            credential_kind=CredentialKind.BROWSER,
        )
        request.state.principal = principal
        return principal

    identity = "same-anonymous-id"
    app.dependency_overrides[authenticate_request] = authenticate_project_a
    first = await client.post(
        URL,
        json={
            "events": [
                canonical_event(
                    "project-a",
                    anonymous_id=identity,
                    message_id="project-a-message",
                )
            ]
        },
        headers=HEADERS,
    )
    app.dependency_overrides[authenticate_request] = authenticate_project_b
    second = await client.post(
        URL,
        json={
            "events": [
                canonical_event(
                    "project-b",
                    anonymous_id=identity,
                    message_id="project-b-message",
                )
            ]
        },
        headers=HEADERS,
    )

    assert first.status_code == 202
    assert second.status_code == 202
    identity_keys = [
        next(key for key in quota_keys(call) if ":identity:" in key)
        for call in quota_calls("event")
    ]
    assert len(identity_keys) == 2
    assert len(set(identity_keys)) == 2
    assert all(identity not in key for key in identity_keys)


def test_rejected_child_bucket_cannot_debit_parent_in_lua_contract():
    check_phase, marker, debit_phase = _HIERARCHICAL_TOKEN_BUCKET_LUA.partition(
        "if rejected_retry_after > 0 then"
    )

    assert marker
    assert "HMGET" in check_phase
    assert "HSET" not in check_phase
    assert debit_phase.index("return {0") < debit_phase.index("HSET")


@pytest.mark.asyncio
async def test_stateful_child_rejection_preserves_parent_balance():
    evaluator = StatefulQuotaEvaluator()
    redis = MagicMock()
    redis.eval = AsyncMock(side_effect=evaluator.evaluate)
    buckets = [
        BucketDebit("parent", BucketLimit(4, 1), 1),
        BucketDebit("child", BucketLimit(1, 1), 1),
    ]

    first = await _admit(redis, buckets, quota_name="Test")
    assert first is None
    assert evaluator.tokens == {"parent": 3, "child": 0}

    before_rejection = dict(evaluator.tokens)
    second = await _admit(redis, buckets, quota_name="Test")

    assert second is not None
    assert second.status_code == 429
    assert evaluator.tokens == before_rejection
    assert redis.eval.await_count == 2


@pytest.mark.asyncio
async def test_request_quota_rejection_has_retry_headers_and_no_publish(client):
    async def reject_rate(script, _numkeys, *_args):
        assert script == _HIERARCHICAL_TOKEN_BUCKET_LUA
        return [0, BROWSER_EVENT_LIMIT.capacity, 0, 3]

    app.state.redis.eval = AsyncMock(side_effect=reject_rate)
    resp = await client.post(
        URL,
        json={"events": [canonical_event("quota-test")]},
        headers=HEADERS,
    )

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3"
    assert resp.headers["X-RateLimit-Limit"] == str(
        BROWSER_EVENT_LIMIT.capacity
    )
    assert resp.headers["X-RateLimit-Remaining"] == "0"
    assert publisher_calls() == []


@pytest.mark.asyncio
async def test_byte_quota_rejection_never_parses_or_publishes(client):
    async def reject_bytes(script, numkeys, *args):
        assert script == _HIERARCHICAL_TOKEN_BUCKET_LUA
        keys = args[:numkeys]
        if any(key.startswith("apdl:rate:byte:") for key in keys):
            return [0, BROWSER_BYTE_LIMIT.capacity, 0, 4]
        return [1, 999, 999, 0]

    app.state.redis.eval = AsyncMock(side_effect=reject_bytes)
    resp = await client.post(
        URL,
        content=b"this is not json",
        headers={**HEADERS, "Content-Type": "application/json"},
    )

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "4"
    assert len(quota_calls("request")) == 1
    assert len(quota_calls("byte")) == 1
    assert quota_calls("event") == []
    assert publisher_calls() == []


@pytest.mark.asyncio
async def test_event_quota_rejection_never_publishes(client):
    async def reject_events(script, numkeys, *args):
        assert script == _HIERARCHICAL_TOKEN_BUCKET_LUA
        keys = args[:numkeys]
        if any(key.startswith("apdl:rate:event:") for key in keys):
            return [0, BROWSER_EVENT_LIMIT.capacity, 0, 2]
        return [1, 999, 999, 0]

    app.state.redis.eval = AsyncMock(side_effect=reject_events)
    resp = await client.post(
        URL,
        json={"events": [canonical_event("quota-test")]},
        headers=HEADERS,
    )

    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "2"
    assert len(quota_calls("request")) == 1
    assert len(quota_calls("event")) == 1
    assert publisher_calls() == []


@pytest.mark.asyncio
async def test_rate_limit_authority_failure_fails_closed(client):
    app.state.redis.eval = AsyncMock(side_effect=ConnectionError("Redis down"))
    resp = await client.post(
        URL,
        json={"events": [canonical_event("quota-test")]},
        headers=HEADERS,
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "service_unavailable"
    assert publisher_calls() == []


@pytest.mark.asyncio
async def test_excessive_json_depth_is_rejected_at_http_boundary(client):
    properties = {"leaf": True}
    for _ in range(11):
        properties = {"nested": properties}
    event = canonical_event("deep", properties=properties)

    resp = await client.post(URL, json={"events": [event]}, headers=HEADERS)

    assert resp.status_code == 400
    assert "maximum JSON depth" in resp.json()["errors"][0]["message"]
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_mixed_valid_and_invalid_events(client):
    """MultipleMixedValidAndInvalidEvents"""
    payload = {
        "events": [
            {"event": "valid_event", "user_id": "usr_1"},
            {"properties": {"no_name": True}},
            {"event": "another_valid", "anonymous_id": "anon_1"},
        ],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"
    # Event at index 1 has no event name/type and no user_id
    found_idx1_error = any(
        "events[1]" in e["field"] for e in body["errors"]
    )
    assert found_idx1_error


@pytest.mark.asyncio
async def test_valid_identify_event(client):
    """ValidIdentifyEvent"""
    event = canonical_event(
        "identify",
        "identify",
        user_id="usr_123",
        traits={"name": "Jane Doe", "email": "jane@example.com"},
    )
    event.pop("anonymous_id")
    payload = {"events": [event]}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 1


@pytest.mark.asyncio
async def test_reject_invalid_properties(client):
    """RejectInvalidProperties"""
    payload = {
        "events": [{
            "event": "test",
            "user_id": "usr_1",
            "properties": "not_an_object",
        }],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_unauthorized_without_api_key(client):
    """Requests without API key should be rejected with 401."""
    class RejectingAuthenticator:
        async def authenticate(self, api_key):
            return None

    app.dependency_overrides.pop(authenticate_request, None)
    app.state.authenticator = RejectingAuthenticator()
    payload = {"events": [canonical_event("test", user_id="u1")]}
    resp = await client.post(URL, json=payload)
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"] == "Valid API key required"


@pytest.mark.asyncio
async def test_unauthorized_with_bad_api_key(client):
    """Requests with malformed API key should be rejected with 401."""
    class RejectingAuthenticator:
        async def authenticate(self, api_key):
            return None

    app.dependency_overrides.pop(authenticate_request, None)
    app.state.authenticator = RejectingAuthenticator()
    payload = {"events": [{"event": "test", "user_id": "u1"}]}
    resp = await client.post(URL, json=payload, headers={"X-API-Key": "bad_key"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["detail"] == "Valid API key required"


@pytest.mark.asyncio
async def test_events_require_write_role(client):
    async def authenticate_without_write_role(request: Request):
        principal = Principal(
            credential_id="read-only",
            project_id="testproj",
            roles=frozenset({"query:read"}),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_without_write_role
    payload = {"events": [{"event": "test", "user_id": "u1"}]}

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Credential requires role: events:write"
    app.state.redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_redis_failure_returns_503(client):
    """When Redis publish fails, endpoint returns 503."""
    async def fail_publish(script, _numkeys, *_args):
        if script == _BOUNDED_XADD_LUA:
            raise ConnectionError("Redis down")
        return [1, 999, 999, 0]

    app.state.redis.eval = AsyncMock(side_effect=fail_publish)
    payload = {"events": [canonical_event("test", user_id="u1")]}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "service_unavailable"


@pytest.mark.asyncio
async def test_ambiguous_redis_failure_never_reports_partial_acceptance(client):
    """A script failure returns one retryable batch failure, never partial 202."""
    async def fail_publish(script, _numkeys, *_args):
        if script == _BOUNDED_XADD_LUA:
            raise ConnectionError("Redis blip")
        return [1, 999, 999, 0]

    app.state.redis.eval = AsyncMock(side_effect=fail_publish)
    payload = {
        "events": [
            canonical_event("e1", user_id="u1", message_id="message-1"),
            canonical_event("e2", user_id="u2", message_id="message-2"),
            canonical_event("e3", user_id="u3", message_id="message-3"),
        ],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "service_unavailable"
    assert "accepted" not in body and "failed" not in body
    calls = publisher_calls()
    assert len(calls) == 1
    assert calls[0].args[3] == 3


@pytest.mark.asyncio
async def test_incomplete_redis_transaction_result_returns_503(client):
    async def incomplete_publish(script, _numkeys, *args):
        if script == _BOUNDED_XADD_LUA:
            return [1, int(args[1])]
        return [1, 999, 999, 0]

    app.state.redis.eval = AsyncMock(side_effect=incomplete_publish)

    resp = await client.post(
        URL,
        json={"events": [canonical_event("incomplete")]},
        headers=HEADERS,
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == "service_unavailable"


@pytest.mark.asyncio
async def test_stream_capacity_rejection_is_retryable_and_never_partial(client):
    async def overloaded_publish(script, _numkeys, *_args):
        if script == _BOUNDED_XADD_LUA:
            return [0, EVENT_STREAM_MAX_ENTRIES]
        return [1, 999, 999, 0]

    app.state.redis.eval = AsyncMock(side_effect=overloaded_publish)
    payload = {
        "events": [
            canonical_event("one", message_id="message-one"),
            canonical_event("two", message_id="message-two"),
        ]
    }

    resp = await client.post(URL, json=payload, headers=HEADERS)

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    assert resp.json() == {
        "error": "service_overloaded",
        "message": "Event persistence backlog is at capacity",
    }
