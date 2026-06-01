import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import evaluate


@pytest.mark.asyncio
async def test_evaluate_requires_internal_token(monkeypatch):
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "secret")

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
    assert response.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_evaluate_server_gate_logs_exposure(monkeypatch):
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "secret")
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
            headers={"X-APDL-Internal-Token": "secret"},
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": {"user_id": "user_123", "attributes": {}},
                "page": "/checkout",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "key": "checkout",
        "value": True,
        "reason": "fallthrough",
        "rule_id": "",
        "bucket": response.json()["bucket"],
        "rollout_percentage": 100.0,
        "bucket_by": "user_id",
        "config_version": 4,
        "source": "server",
    }
    assert response.json()["bucket"] is not None
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
        "value": True,
        "reason": "fallthrough",
        "rule_id": "",
        "bucket": response.json()["bucket"],
        "rollout_percentage": 100.0,
        "bucket_by": "user_id",
        "config_version": 4,
        "source": "server",
        "page": "/checkout",
    }


@pytest.mark.asyncio
async def test_evaluate_rejects_client_only_gate(monkeypatch):
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "secret")
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
            headers={"X-APDL-Internal-Token": "secret"},
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
        "default_value": False,
        "rules": [],
        "fallthrough": {
            "value": True,
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
