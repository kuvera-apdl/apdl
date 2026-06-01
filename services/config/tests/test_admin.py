from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import admin


@pytest.mark.asyncio
async def test_disable_flag_writes_audit_and_broadcasts(monkeypatch):
    existing = make_flag()
    updated = {
        **existing,
        "enabled": False,
        "version": 5,
        "disabled_reason": "guardrail_failed",
        "disabled_by": "system",
        "disabled_at": "2026-06-01T00:00:00+00:00",
    }

    get_flag = AsyncMock(return_value=existing)
    disable_flag = AsyncMock(return_value=updated)
    audit = AsyncMock()
    invalidate = AsyncMock()
    broadcaster = AsyncMock()

    monkeypatch.setattr(admin.pg_store, "get_flag", get_flag)
    monkeypatch.setattr(admin.pg_store, "disable_flag", disable_flag)
    monkeypatch.setattr(admin.pg_store, "create_flag_audit_entry", audit)
    monkeypatch.setattr(admin.redis_cache, "invalidate_flags", invalidate)
    app.state.pg_pool = object()
    app.state.redis = object()
    app.state.broadcaster = broadcaster

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/flags/checkout/disable",
            params={"project_id": "apdl"},
            json={
                "reason": "guardrail_failed",
                "source": "system",
                "evidence": {"metric": "frontend_error_count", "observed": 1},
            },
        )

    assert response.status_code == 200
    assert response.json()["disabled"] is True
    disable_flag.assert_awaited_once_with(
        app.state.pg_pool,
        project_id="apdl",
        key="checkout",
        reason="guardrail_failed",
        source="system",
    )
    audit.assert_awaited_once()
    audit_kwargs = audit.await_args.kwargs
    assert audit_kwargs["action"] == "flag_auto_disabled"
    assert audit_kwargs["evidence"] == {"metric": "frontend_error_count", "observed": 1}
    invalidate.assert_awaited_once_with(app.state.redis, "apdl")
    broadcaster.broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_disable_flag_rejects_flags_without_auto_disable(monkeypatch):
    existing = {**make_flag(), "auto_disable": False}
    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=existing))
    app.state.pg_pool = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/flags/checkout/disable",
            params={"project_id": "apdl"},
            json={"reason": "guardrail_failed", "source": "system", "evidence": {}},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "auto_disable_disabled"


@pytest.mark.asyncio
async def test_disable_flag_allows_admin_source_without_auto_disable(monkeypatch):
    existing = {**make_flag(), "auto_disable": False}
    updated = {
        **existing,
        "enabled": False,
        "version": 5,
        "disabled_reason": "guardrail_failed",
        "disabled_by": "admin",
        "disabled_at": "2026-06-01T00:00:00+00:00",
    }
    disable_flag = AsyncMock(return_value=updated)

    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "disable_flag", disable_flag)
    monkeypatch.setattr(admin.pg_store, "create_flag_audit_entry", AsyncMock())
    monkeypatch.setattr(admin.redis_cache, "invalidate_flags", AsyncMock())
    app.state.pg_pool = object()
    app.state.redis = object()
    app.state.broadcaster = AsyncMock()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/flags/checkout/disable",
            params={"project_id": "apdl"},
            json={"reason": "guardrail_failed", "source": "admin", "evidence": {}},
        )

    assert response.status_code == 200
    assert response.json()["disabled"] is True
    disable_flag.assert_awaited_once()


def make_flag() -> dict:
    return {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "description": "Controls the checkout redesign.",
        "enabled": True,
        "default_value": False,
        "rules": [],
        "fallthrough": {
            "value": False,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "client_exposed": True,
        "auto_disable": True,
        "guardrails": [{
            "metric": "frontend_error_count",
            "threshold": "at_least_one",
            "scope": "page:/checkout",
            "minimum_exposures": 0,
            "window_minutes": 10,
        }],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "version": 4,
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "archived_at": None,
    }
