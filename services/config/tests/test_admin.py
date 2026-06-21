import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import admin


@pytest.mark.asyncio
async def test_flag_update_broadcast_uses_canonical_client_shape():
    broadcaster = AsyncMock()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(broadcaster=broadcaster))
    )
    flag = {**make_flag(), "evaluation_mode": "both"}

    await admin._broadcast_flag_change(
        request,
        "apdl",
        "flag_updated",
        flag,
        "checkout",
    )

    broadcaster.broadcast.assert_awaited_once()
    project_id, event_name, data = broadcaster.broadcast.await_args.args
    payload = json.loads(data)
    assert project_id == "apdl"
    assert event_name == "flag_update"
    assert payload == {
        "action": "flag_updated",
        "flag": admin.serialize_client_flag(flag),
    }
    assert set(payload["flag"]) == {
        "key",
        "enabled",
        "default_variant",
        "variants",
        "salt",
        "rules",
        "fallthrough",
        "version",
    }


@pytest.mark.asyncio
async def test_admin_create_and_update_reject_legacy_boolean_fields():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        create_response = await client.post(
            "/v1/admin/flags",
            params={"project_id": "apdl"},
            json={
                "key": "checkout",
                "name": "Checkout",
                "default_value": False,
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 1},
                    {"key": "treatment", "weight": 1},
                ],
                "fallthrough": {
                    "value": True,
                    "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
                },
            },
        )
        update_response = await client.put(
            "/v1/admin/flags/checkout",
            params={"project_id": "apdl"},
            json={
                "version": 4,
                "default_value": False,
            },
        )

    assert create_response.status_code == 422
    assert update_response.status_code == 422


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


# ---------- Experiments ----------


def make_experiment(overrides: dict | None = None) -> dict:
    exp = {
        "key": "checkout_exp",
        "project_id": "apdl",
        "status": "draft",
        "description": "New checkout",
        "flag_key": "checkout_exp",
        "default_variant": "control",
        "variants_json": '[{"key":"control","weight":1},{"key":"treatment","weight":1}]',
        "targeting_rules_json": "[]",
        "primary_metric_json": "{}",
        "traffic_percentage": 100.0,
        "start_date": "",
        "end_date": "",
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
    }
    if overrides:
        exp.update(overrides)
    return exp


def _experiment_app_state(monkeypatch, broadcaster):
    app.state.pg_pool = object()
    app.state.redis = object()
    app.state.broadcaster = broadcaster
    monkeypatch.setattr(admin.redis_cache, "invalidate_flags", AsyncMock())
    monkeypatch.setattr(admin.redis_cache, "invalidate_experiments", AsyncMock())
    monkeypatch.setattr(admin.pg_store, "create_flag_audit_entry", AsyncMock())


@pytest.mark.asyncio
async def test_create_experiment_initializes_backing_flag(monkeypatch):
    created_flag = {**make_flag(), "key": "checkout_exp", "state": "active", "enabled": True}
    create_flag = AsyncMock(return_value=created_flag)
    create_experiment = AsyncMock(return_value=True)
    broadcaster = AsyncMock()

    monkeypatch.setattr(admin.pg_store, "get_experiment", AsyncMock(return_value=None))
    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=None))
    monkeypatch.setattr(admin.pg_store, "create_flag", create_flag)
    monkeypatch.setattr(admin.pg_store, "create_experiment", create_experiment)
    _experiment_app_state(monkeypatch, broadcaster)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/experiments",
            params={"project_id": "apdl"},
            json={
                "key": "checkout_exp",
                "status": "running",
                "description": "New checkout",
                "variants": [
                    {"key": "control", "weight": 1, "description": "Current"},
                    {"key": "treatment", "weight": 1, "description": "New"},
                ],
                "primary_metric": {"event": "purchase", "type": "conversion", "direction": "increase"},
            },
        )

    assert response.status_code == 201
    assert response.json() == {"created": True, "key": "checkout_exp", "flag_key": "checkout_exp"}

    # Backing flag derived from the experiment and enabled because it is running.
    create_flag.assert_awaited_once()
    flag_arg = create_flag.await_args.args[1]
    assert flag_arg["key"] == "checkout_exp"
    assert flag_arg["state"] == "active"
    assert flag_arg["enabled"] is True
    assert flag_arg["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]
    assert flag_arg["fallthrough"] == {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}}
    assert "salt" in flag_arg

    # Experiment row carries the canonical link + projected fields.
    create_experiment.assert_awaited_once()
    exp_arg = create_experiment.await_args.args[1]
    assert exp_arg["flag_key"] == "checkout_exp"
    assert exp_arg["default_variant"] == "control"
    assert json.loads(exp_arg["primary_metric_json"]) == {
        "event": "purchase",
        "type": "conversion",
        "direction": "increase",
    }

    # Both flag and experiment changes are broadcast.
    events = [call.args[1] for call in broadcaster.broadcast.await_args_list]
    assert "flag_update" in events
    assert "experiment_update" in events


@pytest.mark.asyncio
async def test_create_experiment_conflicts_on_existing_flag(monkeypatch):
    create_flag = AsyncMock()
    monkeypatch.setattr(admin.pg_store, "get_experiment", AsyncMock(return_value=None))
    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=make_flag()))
    monkeypatch.setattr(admin.pg_store, "create_flag", create_flag)
    _experiment_app_state(monkeypatch, AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/experiments",
            params={"project_id": "apdl"},
            json={"key": "checkout_exp", "status": "draft"},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "conflict"
    create_flag.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_experiment_rejects_invalid_variant_weights(monkeypatch):
    create_flag = AsyncMock()
    monkeypatch.setattr(admin.pg_store, "get_experiment", AsyncMock(return_value=None))
    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=None))
    monkeypatch.setattr(admin.pg_store, "create_flag", create_flag)
    _experiment_app_state(monkeypatch, AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/admin/experiments",
            params={"project_id": "apdl"},
            json={
                "key": "checkout_exp",
                "variants": [
                    {"key": "control", "weight": 0},
                    {"key": "treatment", "weight": 0},
                ],
            },
        )

    assert response.status_code == 422
    create_flag.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_experiment_starts_and_enables_flag(monkeypatch):
    existing = make_experiment({"status": "draft"})
    backing = {**make_flag(), "key": "checkout_exp", "state": "draft", "enabled": False, "version": 2}
    updated_flag = {**backing, "state": "active", "enabled": True, "version": 3}
    update_flag = AsyncMock(return_value=updated_flag)
    update_experiment = AsyncMock(return_value=True)

    monkeypatch.setattr(admin.pg_store, "get_experiment", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value=backing))
    monkeypatch.setattr(admin.pg_store, "update_flag", update_flag)
    monkeypatch.setattr(admin.pg_store, "update_experiment", update_experiment)
    _experiment_app_state(monkeypatch, AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/admin/experiments/checkout_exp",
            params={"project_id": "apdl"},
            json={"status": "running"},
        )

    assert response.status_code == 200
    update_flag.assert_awaited_once()
    merged_flag = update_flag.await_args.args[1]
    assert merged_flag["state"] == "active"
    assert merged_flag["enabled"] is True
    assert update_flag.await_args.args[2] == 2  # syncs against the current flag version
    update_experiment.assert_awaited_once()
    assert update_experiment.await_args.args[1]["status"] == "running"


@pytest.mark.asyncio
async def test_update_experiment_rejects_illegal_transition(monkeypatch):
    existing = make_experiment({"status": "completed"})
    get_flag = AsyncMock()
    update_experiment = AsyncMock()

    monkeypatch.setattr(admin.pg_store, "get_experiment", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "get_flag", get_flag)
    monkeypatch.setattr(admin.pg_store, "update_experiment", update_experiment)
    _experiment_app_state(monkeypatch, AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/v1/admin/experiments/checkout_exp",
            params={"project_id": "apdl"},
            json={"status": "running"},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "invalid_transition"
    # Rejected before touching the flag or the experiment row.
    get_flag.assert_not_awaited()
    update_experiment.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_experiment_archives_backing_flag(monkeypatch):
    existing = make_experiment()
    archived = {**make_flag(), "key": "checkout_exp", "state": "archived", "archived_at": "2026-06-02T00:00:00+00:00"}
    archive_flag = AsyncMock(return_value=archived)

    monkeypatch.setattr(admin.pg_store, "get_experiment", AsyncMock(return_value=existing))
    monkeypatch.setattr(admin.pg_store, "delete_experiment", AsyncMock(return_value=True))
    monkeypatch.setattr(admin.pg_store, "get_flag", AsyncMock(return_value={**make_flag(), "key": "checkout_exp"}))
    monkeypatch.setattr(admin.pg_store, "archive_flag", archive_flag)
    _experiment_app_state(monkeypatch, AsyncMock())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            "/v1/admin/experiments/checkout_exp",
            params={"project_id": "apdl"},
        )

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "key": "checkout_exp", "flag_key": "checkout_exp"}
    archive_flag.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_experiments_returns_canonical_record(monkeypatch):
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiments",
        AsyncMock(return_value=[make_experiment({"status": "running"})]),
    )
    app.state.pg_pool = object()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/admin/experiments", params={"project_id": "apdl"})

    assert response.status_code == 200
    entry = response.json()["experiments"][0]
    assert entry["flag_key"] == "checkout_exp"
    assert entry["default_variant"] == "control"
    assert entry["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]
    assert entry["primary_metric"] is None
