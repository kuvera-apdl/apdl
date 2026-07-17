import json
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import flags as flags_router
from app.store.redis_cache import FlagCacheEntry
from app.utils import serialize_flag_collection


@pytest.mark.asyncio
async def test_get_flags_returns_and_caches_canonical_variant_collection(monkeypatch):
    flag = make_flag()
    get_snapshot = AsyncMock(return_value=([flag], 7))
    monkeypatch.setattr(flags_router.pg_store, "get_flag_snapshot", get_snapshot)
    monkeypatch.setattr(
        flags_router.redis_cache,
        "get_flags",
        AsyncMock(return_value=None),
    )
    set_flags = AsyncMock(return_value=True)
    monkeypatch.setattr(flags_router.redis_cache, "set_flags", set_flags)
    redis = object()
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
    get_snapshot.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        client_visible_only=True,
    )
    set_flags.assert_awaited_once_with(
        redis,
        "apdl",
        7,
        json.dumps(expected, separators=(",", ":")),
        ttl=60,
    )


@pytest.mark.asyncio
async def test_get_flags_returns_only_versioned_cache_entry(monkeypatch):
    payload = json.dumps(serialize_flag_collection("apdl", [make_flag()]))
    monkeypatch.setattr(
        flags_router.redis_cache,
        "get_flags",
        AsyncMock(return_value=FlagCacheEntry(4, payload)),
    )
    snapshot = AsyncMock()
    monkeypatch.setattr(flags_router.pg_store, "get_flag_snapshot", snapshot)
    app.state.redis = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/flags")

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "HIT"
    assert response.json() == json.loads(payload)
    snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_cache_population_race_refetches_latest_snapshot(monkeypatch):
    old_flag = make_flag()
    old_flag["version"] = 10
    new_flag = make_flag()
    new_flag["version"] = 11
    get_snapshot = AsyncMock(
        side_effect=[([old_flag], 10), ([new_flag], 11)]
    )
    monkeypatch.setattr(flags_router.pg_store, "get_flag_snapshot", get_snapshot)
    monkeypatch.setattr(
        flags_router.redis_cache,
        "get_flags",
        AsyncMock(return_value=None),
    )
    set_flags = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(flags_router.redis_cache, "set_flags", set_flags)
    app.state.pg_pool = object()
    app.state.redis = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/flags")

    assert response.status_code == 200
    assert response.headers["X-Cache"] == "MISS"
    assert response.json()["flags"][0]["version"] == 11
    assert [call.args[2] for call in set_flags.await_args_list] == [10, 11]
    assert get_snapshot.await_count == 2


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
