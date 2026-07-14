import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import authenticate_request
from app.main import app
from app.routers import evaluate


@pytest.mark.asyncio
async def test_evaluate_requires_api_key():
    class RejectingAuthenticator:
        async def authenticate(self, api_key):
            return None

    app.dependency_overrides.pop(authenticate_request, None)
    app.state.authenticator = RejectingAuthenticator()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/evaluate",
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": {"user_id": "user_123", "attributes": {}},
            },
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Valid API key required"


@pytest.mark.asyncio
async def test_evaluate_server_gate_logs_exposure(monkeypatch):
    monkeypatch.setattr(
        evaluate.pg_store,
        "get_flag",
        AsyncMock(return_value=make_flag({"evaluation_mode": "server"})),
    )
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value=b"1234567890-0")
    app.state.pg_pool = object()
    app.state.redis = redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/evaluate",
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": {"user_id": "user_123", "attributes": {}},
                "page": "/checkout",
                "component": "CheckoutPage",
            },
        )

    assert response.status_code == 200
    response_json = response.json()
    assert response.json() == {
        "key": "checkout",
        "variant": response_json["variant"],
        "reason": "fallthrough",
        "rule_id": None,
        "rollout_bucket": response_json["rollout_bucket"],
        "variant_bucket": response_json["variant_bucket"],
        "rollout_percentage": 100.0,
        "bucket_by": "user_id",
        "config_version": 4,
        "source": "server",
    }
    assert response_json["variant"] in {"control", "treatment"}
    assert response_json["rollout_bucket"] is not None
    assert response_json["variant_bucket"] is not None
    redis.xadd.assert_awaited_once()
    args, kwargs = redis.xadd.await_args
    stream_key, fields = args
    assert stream_key == "events:raw:apdl"
    assert kwargs["maxlen"] == 1000000
    assert kwargs["approximate"] is True

    published = json.loads(fields["event_json"])
    assert published["event"] == "$feature_flag_exposure"
    assert published["type"] == "track"
    assert published["user_id"] == "user_123"
    assert published["session_id"].startswith("server:")
    assert published["properties"] == {
        "flag_key": "checkout",
        "variant": response_json["variant"],
        "reason": "fallthrough",
        "rule_id": None,
        "rollout_bucket": response_json["rollout_bucket"],
        "variant_bucket": response_json["variant_bucket"],
        "rollout_percentage": 100.0,
        "bucket_by": "user_id",
        "config_version": 4,
        "source": "server",
        "page": "/checkout",
        "component": "CheckoutPage",
    }


@pytest.mark.asyncio
async def test_evaluate_server_gate_logs_exposure_with_default_metadata(monkeypatch):
    monkeypatch.setattr(
        evaluate.pg_store,
        "get_flag",
        AsyncMock(return_value=make_flag({"evaluation_mode": "server"})),
    )
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value=b"1234567890-0")
    app.state.pg_pool = object()
    app.state.redis = redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/evaluate",
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": {"anonymous_id": "anon_123", "attributes": {}},
            },
        )

    assert response.status_code == 200
    redis.xadd.assert_awaited_once()

    args, _ = redis.xadd.await_args
    _, fields = args
    published = json.loads(fields["event_json"])
    assert published["anonymous_id"] == "anon_123"
    assert published["message_id"].startswith("srv_")
    assert published["session_id"] == f"server:{published['message_id']}"
    assert published["properties"]["source"] == "server"
    assert published["properties"]["page"] == ""
    assert published["properties"]["component"] == ""
    assert "value" not in published["properties"]
    assert "bucket" not in published["properties"]


@pytest.mark.asyncio
async def test_evaluate_rejects_client_only_gate(monkeypatch):
    monkeypatch.setattr(
        evaluate.pg_store,
        "get_flag",
        AsyncMock(return_value=make_flag({"evaluation_mode": "client"})),
    )
    redis = AsyncMock()
    redis.xadd = AsyncMock()
    app.state.pg_pool = object()
    app.state.redis = redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/evaluate",
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": {"user_id": "user_123", "attributes": {}},
            },
        )

    assert response.status_code == 403
    assert response.json()["error"] == "invalid_evaluation_mode"
    redis.xadd.assert_not_awaited()


def make_flag(overrides: dict | None = None) -> dict:
    flag = {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "state": "active",
        "owners": ["team-growth"],
        "review_by": "2099-07-01",
        "description": "Controls checkout.",
        "enabled": True,
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "evaluation_mode": "server",
        "auto_disable": True,
        "guardrails": [],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "version": 4,
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "archived_at": None,
    }
    if overrides:
        flag.update(overrides)
    return flag
