"""Port of C++ test_events_handler.cpp to Python/pytest.

Tests the POST /v1/events endpoint via httpx AsyncClient against the FastAPI
app with a mock Redis backend.
"""

import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app

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


@pytest.fixture(autouse=True)
def _setup_mock_redis():
    """Inject a mock Redis into app.state before each test."""
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    mock_redis.xadd = AsyncMock(return_value=b"1234567890-0")
    app.state.redis = mock_redis
    # Reset rate-limit buckets between tests so they don't interfere
    from app.middleware import rate_limit
    rate_limit._buckets.clear()
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
    app.state.redis.xadd.assert_awaited_once()

    args, kwargs = app.state.redis.xadd.await_args
    stream_key, fields = args
    assert stream_key == "events:raw:testproj"
    assert kwargs["maxlen"] == 1000000
    assert kwargs["approximate"] is True

    published = json.loads(fields["event_json"])
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
    assert app.state.redis.xadd.await_count == 4

    published = [
        json.loads(call.args[1]["event_json"])
        for call in app.state.redis.xadd.await_args_list
    ]
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
    serialized = "".join(
        call.args[1]["event_json"] for call in app.state.redis.xadd.await_args_list
    )
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
    app.state.redis.xadd.assert_awaited_once()

    args, _ = app.state.redis.xadd.await_args
    _, fields = args
    published = json.loads(fields["event_json"])
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
    app.state.redis.xadd.assert_not_awaited()


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
    app.state.redis.xadd.assert_not_awaited()


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
    payload = {"events": [canonical_event(
        "identify",
        "identify",
        user_id="usr_123",
        anonymous_id=None,
        traits={"name": "Jane Doe", "email": "jane@example.com"},
    )]}
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
    app.state.redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_failure_returns_503(client):
    """When Redis publish fails, endpoint returns 503."""
    app.state.redis.xadd = AsyncMock(side_effect=ConnectionError("Redis down"))
    payload = {"events": [canonical_event("test", user_id="u1")]}
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "service_unavailable"


@pytest.mark.asyncio
async def test_partial_redis_failure(client):
    """When some events fail to publish, response includes both accepted and failed."""
    call_count = 0

    async def flaky_xadd(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ConnectionError("Redis blip")
        return b"1234567890-0"

    app.state.redis.xadd = flaky_xadd
    payload = {
        "events": [
            canonical_event("e1", user_id="u1", message_id="message-1"),
            canonical_event("e2", user_id="u2", message_id="message-2"),
            canonical_event("e3", user_id="u3", message_id="message-3"),
        ],
    }
    resp = await client.post(URL, json=payload, headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 2
    assert body["failed"] == 1
