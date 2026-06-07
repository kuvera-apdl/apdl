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
        "state": "disabled",
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
        "state": "disabled",
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


@pytest.mark.asyncio
async def test_update_flag_rejects_variants_without_existing_default(monkeypatch):
    existing = make_flag()
    update_flag = AsyncMock()

    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "update_flag", update_flag)
    app.state.pg_pool = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/admin/flags/checkout",
            params={"project_id": "apdl"},
            json={
                "version": 4,
                "variants": [{"key": "treatment", "weight": 1}],
            },
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    update_flag.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_flag_rejects_default_variant_without_matching_existing_variant(monkeypatch):
    existing = make_flag()
    update_flag = AsyncMock()

    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "update_flag", update_flag)
    app.state.pg_pool = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/admin/flags/checkout",
            params={"project_id": "apdl"},
            json={
                "version": 4,
                "default_variant": "missing",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    update_flag.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_flags_reports_review_and_cleanup_candidates(monkeypatch):
    reviewed = make_flag()
    cleanup_candidate = {
        **make_flag(),
        "key": "checkout-v2",
        "variants": [
            {"key": "control", "weight": 0},
            {"key": "treatment", "weight": 1},
        ],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        },
    }
    missing_owner = {
        **make_flag(),
        "key": "search-v2",
        "owners": [],
        "review_by": None,
    }
    monkeypatch.setattr(
        admin.pg_store,
        "get_flags",
        AsyncMock(return_value=[reviewed, cleanup_candidate, missing_owner]),
    )
    app.state.pg_pool = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/admin/flags/stale",
            params={"project_id": "apdl"},
        )

    assert response.status_code == 200
    body = response.json()
    reasons_by_key = {
        flag["key"]: flag["stale_reasons"]
        for flag in body["flags"]
    }
    assert "fully_rolled_out" in reasons_by_key["checkout-v2"]
    assert reasons_by_key["search-v2"] == ["missing_owner", "missing_review_date"]


@pytest.mark.asyncio
async def test_cleanup_flag_archives_full_rollout_and_writes_audit(monkeypatch):
    existing = {
        **make_flag(),
        "variants": [
            {"key": "control", "weight": 0},
            {"key": "treatment", "weight": 1},
        ],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        },
    }
    archived = {
        **existing,
        "state": "archived",
        "enabled": False,
        "version": 5,
        "archived_at": "2026-06-01T00:00:00+00:00",
    }
    archive_flag = AsyncMock(return_value=archived)
    audit = AsyncMock()
    invalidate = AsyncMock()
    broadcaster = AsyncMock()

    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "archive_flag", archive_flag)
    monkeypatch.setattr(admin.pg_store, "create_flag_audit_entry", audit)
    monkeypatch.setattr(admin.redis_cache, "invalidate_flags", invalidate)
    app.state.pg_pool = object()
    app.state.redis = object()
    app.state.broadcaster = broadcaster

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/flags/checkout/cleanup",
            params={"project_id": "apdl"},
            json={
                "version": 4,
                "source": "admin",
                "evidence": {"ticket": "PD-123"},
            },
        )

    assert response.status_code == 200
    assert response.json()["cleaned_up"] is True
    archive_flag.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        "checkout",
        expected_version=4,
    )
    audit.assert_awaited_once()
    audit_kwargs = audit.await_args.kwargs
    assert audit_kwargs["action"] == "flag_cleanup_archived"
    assert audit_kwargs["reason"] == "fully_rolled_out"
    assert audit_kwargs["evidence"] == {
        "ticket": "PD-123",
        "cleanup_reasons": ["fully_rolled_out"],
    }
    invalidate.assert_awaited_once_with(app.state.redis, "apdl")
    broadcaster.broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_flag_audit_returns_entries_for_archived_flag(monkeypatch):
    archived = {**make_flag(), "state": "archived", "archived_at": "2026-06-01T00:00:00+00:00"}
    audit_entries = [{
        "id": 1,
        "project_id": "apdl",
        "flag_key": "checkout",
        "action": "flag_archived",
        "actor": "admin",
        "previous_version": 4,
        "new_version": 5,
        "before": make_flag(),
        "after": archived,
        "evidence": {},
        "reason": "",
        "created_at": "2026-06-01T00:00:00+00:00",
    }]

    get_flag = AsyncMock(return_value=archived)
    get_entries = AsyncMock(return_value=audit_entries)
    monkeypatch.setattr(admin.pg_store, "get_flag", get_flag)
    monkeypatch.setattr(admin.pg_store, "get_flag_audit_entries", get_entries)
    app.state.pg_pool = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/admin/flags/checkout/audit",
            params={"project_id": "apdl", "limit": 25},
        )

    assert response.status_code == 200
    assert response.json()["audit"] == audit_entries
    get_flag.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        "checkout",
        include_archived=True,
    )
    get_entries.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        "checkout",
        limit=25,
    )


def make_flag() -> dict:
    return {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "state": "active",
        "owners": ["team-growth"],
        "review_by": "2099-07-01",
        "description": "Controls the checkout redesign.",
        "enabled": True,
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "evaluation_mode": "client",
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
