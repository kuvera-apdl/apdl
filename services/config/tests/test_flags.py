import json
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import flags as flags_router
from app.utils import serialize_flag_collection


@pytest.mark.asyncio
async def test_get_flags_returns_and_caches_canonical_variant_collection(monkeypatch):
    flag = make_flag()
    get_flags = AsyncMock(return_value=[flag])
    monkeypatch.setattr(flags_router.pg_store, "get_flags", get_flags)
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    app.state.pg_pool = object()
    app.state.redis = redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/flags",
            params={"project_id": "apdl"},
        )

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    expected = serialize_flag_collection("apdl", [flag])
    assert response.json() == expected
    get_flags.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        client_visible_only=True,
    )
    redis.set.assert_awaited_once()
    _, cached_payload = redis.set.await_args.args[:2]
    assert json.loads(cached_payload) == expected


@pytest.mark.asyncio
async def test_flag_read_denies_cross_tenant_project():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/flags", params={"project_id": "other"})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_routes_require_config_write_role():
    async def authenticate_reader(request: Request):
        principal = Principal(
            credential_id="reader",
            project_id="apdl",
            roles=frozenset({"config:read"}),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_reader
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/admin/flags", params={"project_id": "apdl"})

    assert response.status_code == 403


def make_flag() -> dict:
    return {
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
        "evaluation_mode": "client",
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
