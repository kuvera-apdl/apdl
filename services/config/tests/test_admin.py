"""HTTP contract tests for atomic Config administration."""

import json
from unittest.mock import AsyncMock

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import admin
from app.store import mutations


VALID_STATISTICAL_PLAN = {
    "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
    "baseline_conversion_rate": 0.5,
    "minimum_detectable_effect": 0.5,
    "significance_level": 0.05,
    "nominal_power": 0.8,
    "required_sample_size_per_arm": 20,
    "data_settlement_seconds": 5,
}


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
    if overrides:
        flag.update(overrides)
    return flag


def make_experiment(overrides: dict | None = None) -> dict:
    experiment = {
        "key": "checkout_exp",
        "project_id": "apdl",
        "status": "draft",
        "description": "New checkout",
        "flag_key": "checkout_exp",
        "default_variant": "control",
        "variants_json": (
            '[{"key":"control","weight":1},'
            '{"key":"treatment","weight":1}]'
        ),
        "targeting_rules_json": "[]",
        "primary_metric_json": "{}",
        "statistical_plan": None,
        "traffic_percentage": 100.0,
        "start_date": None,
        "end_date": None,
        "version": 3,
        "creation_idempotency_key": None,
        "creation_idempotency_request_sha256": None,
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
    }
    if overrides:
        experiment.update(overrides)
    return experiment


async def _request(
    method: str, path: str, *, json_body=None, params=None, headers=None
):
    app.state.pg_pool = object()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(
            method, path, json=json_body, params=params, headers=headers
        )


@pytest.mark.asyncio
async def test_admin_rejects_legacy_and_generic_lifecycle_fields():
    create_response = await _request(
        "POST",
        "/v1/admin/flags",
        params={"project_id": "apdl"},
        json_body={
            "key": "checkout",
            "name": "Checkout",
            "default_value": False,
            "default_variant": "control",
            "variants": [{"key": "control", "weight": 1}],
            "fallthrough": {
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
            },
        },
    )
    update_response = await _request(
        "PUT",
        "/v1/admin/flags/checkout",
        params={"project_id": "apdl"},
        json_body={"version": 4, "state": "disabled", "enabled": False},
    )

    assert create_response.status_code == 422
    assert update_response.status_code == 422


@pytest.mark.asyncio
async def test_update_flag_delegates_to_atomic_authority(monkeypatch):
    updated = make_flag({"description": "updated", "version": 5})
    command = AsyncMock(return_value=updated)
    monkeypatch.setattr(admin.mutations, "update_standalone_flag", command)

    response = await _request(
        "PUT",
        "/v1/admin/flags/checkout",
        params={"project_id": "apdl"},
        json_body={"version": 4, "description": "updated"},
    )

    assert response.status_code == 200
    command.assert_awaited_once_with(
        app.state.pg_pool,
        project_id="apdl",
        key="checkout",
        expected_version=4,
        updates={"description": "updated"},
        actor="credential:test-config",
    )


@pytest.mark.asyncio
async def test_every_generic_owned_flag_mutation_maps_to_409(monkeypatch):
    error = mutations.ExperimentOwnedFlagError("checkout", "checkout_exp")
    command = AsyncMock(side_effect=error)
    monkeypatch.setattr(admin.mutations, "transition_standalone_flag", command)

    response = await _request(
        "POST",
        "/v1/admin/flags/checkout/transition",
        params={"project_id": "apdl"},
        json_body={"version": 4, "target_state": "draft"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "experiment_managed_flag",
        "message": "Flag 'checkout' is managed by experiment 'checkout_exp'",
        "experiment_key": "checkout_exp",
    }


@pytest.mark.asyncio
async def test_disable_uses_authenticated_actor_and_rejects_source(monkeypatch):
    command = AsyncMock(return_value=(make_flag({"state": "disabled"}), True))
    monkeypatch.setattr(admin.mutations, "disable_standalone_flag", command)

    spoofed = await _request(
        "POST",
        "/v1/admin/flags/checkout/disable",
        params={"project_id": "apdl"},
        json_body={"version": 4, "source": "system"},
    )
    accepted = await _request(
        "POST",
        "/v1/admin/flags/checkout/disable",
        params={"project_id": "apdl"},
        json_body={
            "version": 4,
            "reason": "guardrail_failed",
            "evidence": {"metric": "frontend_error_count"},
        },
    )

    assert spoofed.status_code == 422
    assert accepted.status_code == 200
    command.assert_awaited_once_with(
        app.state.pg_pool,
        project_id="apdl",
        key="checkout",
        expected_version=4,
        reason="guardrail_failed",
        evidence={"metric": "frontend_error_count"},
        actor="credential:test-config",
    )


@pytest.mark.asyncio
async def test_cleanup_uses_atomic_authority(monkeypatch):
    archived = make_flag(
        {"state": "archived", "enabled": False, "version": 5}
    )
    command = AsyncMock(return_value=(archived, ["fully_rolled_out"]))
    monkeypatch.setattr(admin.mutations, "cleanup_standalone_flag", command)

    response = await _request(
        "POST",
        "/v1/admin/flags/checkout/cleanup",
        params={"project_id": "apdl"},
        json_body={"version": 4, "evidence": {"ticket": "PD-123"}},
    )

    assert response.status_code == 200
    assert response.json()["cleanup_reasons"] == ["fully_rolled_out"]


@pytest.mark.asyncio
async def test_archive_flag_requires_and_forwards_version(monkeypatch):
    command = AsyncMock(
        return_value=make_flag({"state": "archived", "enabled": False})
    )
    monkeypatch.setattr(admin.mutations, "archive_standalone_flag", command)

    missing = await _request(
        "DELETE",
        "/v1/admin/flags/checkout",
        params={"project_id": "apdl"},
    )
    archived = await _request(
        "DELETE",
        "/v1/admin/flags/checkout",
        params={"project_id": "apdl", "version": 4},
    )

    assert missing.status_code == 422
    assert archived.status_code == 200
    command.assert_awaited_once_with(
        app.state.pg_pool,
        project_id="apdl",
        key="checkout",
        expected_version=4,
        actor="credential:test-config",
    )


@pytest.mark.asyncio
async def test_stale_flags_reports_review_and_cleanup_candidates(monkeypatch):
    cleanup = make_flag(
        {
            "key": "checkout-v2",
            "variants": [
                {"key": "control", "weight": 0},
                {"key": "treatment", "weight": 1},
            ],
        }
    )
    missing_owner = make_flag(
        {"key": "search-v2", "owners": [], "review_by": None}
    )
    monkeypatch.setattr(
        admin.pg_store,
        "get_flags",
        AsyncMock(return_value=[cleanup, missing_owner]),
    )

    response = await _request(
        "GET",
        "/v1/admin/flags/stale",
        params={"project_id": "apdl"},
    )

    reasons = {entry["key"]: entry["stale_reasons"] for entry in response.json()["flags"]}
    assert "fully_rolled_out" in reasons["checkout-v2"]
    assert reasons["search-v2"] == ["missing_owner", "missing_review_date"]


@pytest.mark.asyncio
async def test_create_experiment_delegates_one_bundle_and_omits_presence_value(
    monkeypatch,
):
    command = AsyncMock(
        return_value=(make_experiment({"version": 1}), make_flag())
    )
    monkeypatch.setattr(admin.mutations, "create_experiment_bundle", command)

    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        json_body={
            "key": "checkout_exp",
            "status": "draft",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
            "start_date": "2026-08-01T00:00:00Z",
            "end_date": "2026-08-31T00:00:00Z",
            "primary_metric": {"event": "purchase"},
            "targeting_rules": [
                {
                    "id": "has-plan",
                    "conditions": [{"attribute": "plan", "operator": "exists"}],
                    "rollout": {"percentage": 100, "bucket_by": "user_id"},
                }
            ],
        },
    )

    assert response.status_code == 201
    kwargs = command.await_args.kwargs
    stored_rules = json.loads(kwargs["experiment"]["targeting_rules_json"])
    assert stored_rules[0]["conditions"][0] == {
        "attribute": "plan",
        "operator": "exists",
    }
    assert kwargs["actor"] == "credential:test-config"


@pytest.mark.asyncio
async def test_create_experiment_persists_and_reuses_idempotency_key(monkeypatch):
    key = "11111111-1111-4111-8111-111111111111:22222222-2222-4222-8222-222222222222"
    command = AsyncMock(
        return_value=(make_experiment({"version": 1}), make_flag())
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(admin.mutations, "create_experiment_bundle", command)
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment_by_creation_idempotency_key",
        lookup,
    )
    payload = {
        "key": "checkout_exp",
        "status": "draft",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
    }

    first = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        headers={"Idempotency-Key": key},
        json_body=payload,
    )

    assert first.status_code == 201
    assert command.await_args.kwargs["experiment"]["creation_idempotency_key"] == key
    request_sha256 = command.await_args.kwargs["experiment"][
        "creation_idempotency_request_sha256"
    ]
    assert len(request_sha256) == 64

    lookup.return_value = make_experiment(
        {
            "version": 1,
            "creation_idempotency_key": key,
            "creation_idempotency_request_sha256": request_sha256,
        }
    )
    retried = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        headers={"Idempotency-Key": key},
        json_body=payload,
    )
    assert retried.status_code == 200
    assert retried.json() == {
        "created": True,
        "key": "checkout_exp",
        "flag_key": "checkout_exp",
        "version": 1,
    }
    assert command.await_count == 1


@pytest.mark.asyncio
async def test_create_experiment_reconciles_a_concurrent_idempotent_insert(
    monkeypatch,
):
    key = "test:experiment:concurrent"
    payload = {
        "key": "checkout_exp",
        "status": "draft",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
    }
    request_sha256 = admin._experiment_creation_request_sha256(
        "apdl",
        admin.ExperimentCreate.model_validate(payload),
    )
    existing = make_experiment(
        {
            "version": 1,
            "creation_idempotency_key": key,
            "creation_idempotency_request_sha256": request_sha256,
        }
    )
    lookup = AsyncMock(side_effect=[None, existing])
    command = AsyncMock(side_effect=asyncpg.UniqueViolationError("duplicate"))
    monkeypatch.setattr(admin.mutations, "create_experiment_bundle", command)
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment_by_creation_idempotency_key",
        lookup,
    )

    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        headers={"Idempotency-Key": key},
        json_body=payload,
    )

    assert response.status_code == 200
    assert response.json()["key"] == "checkout_exp"


@pytest.mark.asyncio
async def test_create_experiment_rejects_idempotency_key_reuse_for_changed_request(
    monkeypatch,
):
    key = "test:experiment:create"
    command = AsyncMock()
    monkeypatch.setattr(admin.mutations, "create_experiment_bundle", command)
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment_by_creation_idempotency_key",
        AsyncMock(
            return_value=make_experiment(
                {
                    "creation_idempotency_key": key,
                    "creation_idempotency_request_sha256": "0" * 64,
                }
            )
        ),
    )

    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        headers={"Idempotency-Key": key},
        json_body={
            "key": "checkout_exp",
            "status": "draft",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == "idempotency_conflict"
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_experiment_rejects_noncanonical_idempotency_key(monkeypatch):
    command = AsyncMock()
    monkeypatch.setattr(admin.mutations, "create_experiment_bundle", command)
    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        headers={"Idempotency-Key": " contains whitespace"},
        json_body={
            "key": "checkout_exp",
            "status": "draft",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
        },
    )
    assert response.status_code == 422
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_experiment_requires_running_decision_contract():
    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        json_body={
            "key": "checkout_exp",
            "status": "running",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_running_experiment_persists_valid_statistical_plan(
    monkeypatch,
):
    command = AsyncMock(
        return_value=(make_experiment({"status": "running", "version": 1}), make_flag())
    )
    monkeypatch.setattr(admin.mutations, "create_experiment_bundle", command)

    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        json_body={
            "key": "checkout_exp",
            "status": "running",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
            "start_date": "2026-07-01T00:00:00Z",
            "end_date": "2026-08-01T00:00:00Z",
            "primary_metric": {
                "event": "purchase",
                "type": "conversion",
                "direction": "increase",
            },
            "statistical_plan": VALID_STATISTICAL_PLAN,
        },
    )

    assert response.status_code == 201
    assert (
        command.await_args.kwargs["experiment"]["statistical_plan"]
        == VALID_STATISTICAL_PLAN
    )


@pytest.mark.asyncio
async def test_create_experiment_rejects_understated_predeclared_sample_target():
    response = await _request(
        "POST",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
        json_body={
            "key": "checkout_exp",
            "status": "running",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
            "start_date": "2026-07-01T00:00:00Z",
            "end_date": "2026-08-01T00:00:00Z",
            "primary_metric": {
                "event": "purchase",
                "type": "conversion",
                "direction": "increase",
            },
            "statistical_plan": {
                **VALID_STATISTICAL_PLAN,
                "required_sample_size_per_arm": 2,
            },
        },
    )

    assert response.status_code == 422
    assert "required_sample_size_per_arm" in response.text


@pytest.mark.asyncio
async def test_update_experiment_rejects_stale_version(monkeypatch):
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment",
        AsyncMock(return_value=make_experiment({"version": 4})),
    )

    response = await _request(
        "PUT",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl"},
        json_body={"version": 3, "description": "stale"},
    )

    assert response.status_code == 409
    assert response.json()["current_version"] == 4


@pytest.mark.asyncio
async def test_update_experiment_honors_explicit_nullable_clears(monkeypatch):
    existing = make_experiment(
        {
            "start_date": "2026-01-01T00:00:00+00:00",
            "end_date": "2099-01-01T00:00:00+00:00",
            "primary_metric_json": '{"event":"purchase"}',
        }
    )
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment",
        AsyncMock(return_value=existing),
    )
    command = AsyncMock(
        return_value=(make_experiment({"version": 4}), make_flag())
    )
    monkeypatch.setattr(admin.mutations, "update_experiment_bundle", command)

    response = await _request(
        "PUT",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl"},
        json_body={
            "version": 3,
            "start_date": None,
            "end_date": None,
            "primary_metric": None,
        },
    )

    assert response.status_code == 200
    desired = command.await_args.kwargs["desired"]
    assert desired["start_date"] is None
    assert desired["end_date"] is None
    assert desired["primary_metric_json"] == "{}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("default_variant", "treatment"),
        (
            "variants",
            [
                {"key": "control", "weight": 1},
                {"key": "new-treatment", "weight": 1},
            ],
        ),
        ("primary_metric", {"event": "checkout_completed", "type": "conversion"}),
        ("statistical_plan", VALID_STATISTICAL_PLAN),
        ("start_date", "2026-07-02T00:00:00Z"),
        ("end_date", "2026-08-02T00:00:00Z"),
    ],
)
async def test_update_experiment_freezes_analysis_fields_after_draft(
    monkeypatch,
    field,
    value,
):
    existing = make_experiment(
        {
            "status": "running",
            "start_date": "2026-07-01T00:00:00+00:00",
            "end_date": "2026-08-01T00:00:00+00:00",
            "primary_metric_json": (
                '{"event":"purchase","type":"conversion"}'
            ),
        }
    )
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment",
        AsyncMock(return_value=existing),
    )
    command = AsyncMock()
    monkeypatch.setattr(admin.mutations, "update_experiment_bundle", command)

    response = await _request(
        "PUT",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl"},
        json_body={"version": 3, field: value},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "immutable_experiment_contract"
    assert response.json()["fields"] == [field]
    command.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_experiment_allows_status_only_transition_after_draft(monkeypatch):
    existing = make_experiment(
        {
            "status": "running",
            "start_date": "2026-07-01T00:00:00+00:00",
            "end_date": "2026-08-01T00:00:00+00:00",
            "primary_metric_json": (
                '{"event":"purchase","type":"conversion"}'
            ),
        }
    )
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment",
        AsyncMock(return_value=existing),
    )
    command = AsyncMock(
        return_value=(make_experiment({"status": "stopped", "version": 4}), make_flag())
    )
    monkeypatch.setattr(admin.mutations, "update_experiment_bundle", command)

    response = await _request(
        "PUT",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl"},
        json_body={"version": 3, "status": "stopped"},
    )

    assert response.status_code == 200
    desired = command.await_args.kwargs["desired"]
    assert desired["status"] == "stopped"
    assert desired["default_variant"] == "control"
    assert desired["variants_json"] == existing["variants_json"]
    assert desired["primary_metric_json"] == existing["primary_metric_json"]
    assert desired["start_date"] == existing["start_date"]
    assert desired["end_date"] == existing["end_date"]


@pytest.mark.asyncio
async def test_update_experiment_rejects_illegal_transition(monkeypatch):
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiment",
        AsyncMock(return_value=make_experiment({"status": "completed"})),
    )

    response = await _request(
        "PUT",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl"},
        json_body={"version": 3, "status": "running"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "invalid_transition"


@pytest.mark.asyncio
async def test_delete_experiment_requires_and_forwards_version(monkeypatch):
    command = AsyncMock(return_value=(make_experiment({"version": 4}), make_flag()))
    monkeypatch.setattr(admin.mutations, "delete_experiment_bundle", command)

    missing = await _request(
        "DELETE",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl"},
    )
    deleted = await _request(
        "DELETE",
        "/v1/admin/experiments/checkout_exp",
        params={"project_id": "apdl", "version": 3},
    )

    assert missing.status_code == 422
    assert deleted.status_code == 200
    command.assert_awaited_once_with(
        app.state.pg_pool,
        project_id="apdl",
        key="checkout_exp",
        expected_version=3,
        actor="credential:test-config",
    )


@pytest.mark.asyncio
async def test_list_experiments_serializes_datetimes_and_version(monkeypatch):
    monkeypatch.setattr(
        admin.pg_store,
        "get_experiments",
        AsyncMock(
            return_value=[
                make_experiment(
                    {
                        "start_date": "2026-06-01 00:00:00+00:00",
                        "end_date": "2026-07-01 00:00:00+00:00",
                    }
                )
            ]
        ),
    )

    response = await _request(
        "GET",
        "/v1/admin/experiments",
        params={"project_id": "apdl"},
    )

    entry = response.json()["experiments"][0]
    assert entry["start_date"] == "2026-06-01T00:00:00+00:00"
    assert entry["end_date"] == "2026-07-01T00:00:00+00:00"
    assert entry["version"] == 3
