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
    enqueue = AsyncMock()
    monkeypatch.setattr(evaluate.mutations, "enqueue_exposure", enqueue)
    app.state.pg_pool = object()

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
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert enqueue.await_args.args == (app.state.pg_pool,)
    assert kwargs["stream_key"] == "events:raw:apdl"
    published = kwargs["event"]
    assert published["event"] == "$feature_flag_exposure"
    assert published["type"] == "track"
    assert published["user_id"] == "user_123"
    assert published["session_id"].startswith("server:")
    assert published["context"] == {
        "library": {"name": "apdl-config", "version": "server"}
    }
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
    enqueue = AsyncMock()
    monkeypatch.setattr(evaluate.mutations, "enqueue_exposure", enqueue)
    app.state.pg_pool = object()

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
    enqueue.assert_awaited_once()
    published = enqueue.await_args.kwargs["event"]
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
    enqueue = AsyncMock()
    monkeypatch.setattr(evaluate.mutations, "enqueue_exposure", enqueue)
    app.state.pg_pool = object()

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
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_evaluate_requires_identity_when_logging_exposure(monkeypatch):
    get_flag = AsyncMock()
    monkeypatch.setattr(evaluate.pg_store, "get_flag", get_flag)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/evaluate",
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": {"attributes": {}},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"] == "identity_required"
    get_flag.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_field, bucket_field",
    [
        ("user_id", "anonymous_id"),
        ("anonymous_id", "user_id"),
    ],
)
@pytest.mark.parametrize(
    "operator, explicit_empty, expected_reason",
    [
        ("not_exists", False, "rule_match"),
        ("exists", False, "fallthrough_rollout"),
        ("exists", True, "rule_match"),
        ("not_exists", True, "fallthrough_rollout"),
    ],
)
async def test_evaluate_preserves_omitted_and_empty_identity_presence(
    monkeypatch,
    identity_field,
    bucket_field,
    operator,
    explicit_empty,
    expected_reason,
):
    flag = make_flag(
        {
            "rules": [
                {
                    "id": "identity-presence",
                    "name": "Identity presence",
                    "conditions": [
                        {"attribute": identity_field, "operator": operator},
                    ],
                    "rollout": {
                        "percentage": 100.0,
                        "bucket_by": bucket_field,
                    },
                },
            ],
            "fallthrough": {
                "rollout": {
                    "percentage": 0.0,
                    "bucket_by": bucket_field,
                },
            },
        }
    )
    monkeypatch.setattr(
        evaluate.pg_store,
        "get_flag",
        AsyncMock(return_value=flag),
    )
    app.state.pg_pool = object()
    context = {bucket_field: "fixture-unit", "attributes": {}}
    if explicit_empty:
        context[identity_field] = ""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/evaluate",
            json={
                "project_id": "apdl",
                "key": "checkout",
                "context": context,
                "log_exposure": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["reason"] == expected_reason


@pytest.mark.asyncio
async def test_evaluate_fails_closed_when_exposure_intent_cannot_persist(monkeypatch):
    monkeypatch.setattr(
        evaluate.pg_store,
        "get_flag",
        AsyncMock(return_value=make_flag({"evaluation_mode": "server"})),
    )
    monkeypatch.setattr(
        evaluate.mutations,
        "enqueue_exposure",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    app.state.pg_pool = object()

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

    assert response.status_code == 503
    assert response.json()["error"] == "exposure_persistence_unavailable"


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
        "auto_disable": False,
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
