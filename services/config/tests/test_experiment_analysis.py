"""HTTP contract tests for authoritative experiment analysis metadata."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import experiments


FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "experiments"
    / "three-arm-analysis.json"
)


@pytest.fixture(autouse=True)
def query_read_request_context():
    async def authenticate_query_request(request: Request):
        principal = Principal(
            credential_id="test-query",
            project_id="apdl",
            roles=frozenset({"query:read"}),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_query_request
    yield
    app.dependency_overrides.pop(authenticate_request, None)


def make_experiment(overrides: dict | None = None) -> dict:
    experiment = {
        "key": "checkout_exp",
        "project_id": "apdl",
        "status": "running",
        "description": "New checkout",
        "flag_key": "checkout_gate",
        "default_variant": "control",
        "variants_json": (
            '[{"key":"control","weight":1},'
            '{"key":"treatment","weight":1}]'
        ),
        "targeting_rules_json": "[]",
        "primary_metric_json": (
            '{"event":"purchase","type":"conversion","direction":"increase"}'
        ),
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.5,
            "minimum_detectable_effect": 0.5,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 20,
            "data_settlement_seconds": 5,
        },
        "traffic_percentage": 100.0,
        "start_date": "2026-08-01T00:00:00+00:00",
        "end_date": "2026-08-31T00:00:00+00:00",
        "version": 7,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
    }
    if overrides:
        experiment.update(overrides)
    return experiment


async def _get(path: str):
    app.state.pg_pool = object()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["scheduled", "running", "completed", "stopped"])
async def test_analysis_returns_exact_authoritative_contract(monkeypatch, status):
    get_experiment = AsyncMock(return_value=make_experiment({"status": status}))
    monkeypatch.setattr(experiments.pg_store, "get_experiment", get_experiment)

    response = await _get("/v1/experiments/checkout_exp/analysis")

    assert response.status_code == 200
    assert response.json() == {
        "key": "checkout_exp",
        "flag_key": "checkout_gate",
        "status": status,
        "control_variant": "control",
        "variants": ["control", "treatment"],
        "metric_event": "purchase",
        "metric_direction": "increase",
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.5,
            "minimum_detectable_effect": 0.5,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 20,
            "data_settlement_seconds": 5,
        },
        "start_date": "2026-08-01T00:00:00Z",
        "end_date": "2026-08-31T00:00:00Z",
        "version": 7,
    }
    get_experiment.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        "checkout_exp",
    )


@pytest.mark.asyncio
async def test_analysis_emits_shared_three_arm_fixture_contract(monkeypatch):
    fixture = json.loads(FIXTURE_PATH.read_text())
    contract = fixture["config_contract"]
    stored_variants = [
        {"key": key, "weight": 1, "description": ""}
        for key in contract["variants"]
    ]
    experiment = make_experiment(
        {
            "key": contract["key"],
            "flag_key": contract["flag_key"],
            "status": contract["status"],
            "default_variant": contract["control_variant"],
            "variants_json": json.dumps(stored_variants, separators=(",", ":")),
            "primary_metric_json": json.dumps(
                {
                    "event": contract["metric_event"],
                    "type": "conversion",
                    "direction": "increase",
                },
                separators=(",", ":"),
            ),
            "start_date": contract["start_date"],
            "end_date": contract["end_date"],
            "version": contract["version"],
        }
    )
    monkeypatch.setattr(
        experiments.pg_store,
        "get_experiment",
        AsyncMock(return_value=experiment),
    )

    response = await _get(
        f"/v1/experiments/{contract['key']}/analysis"
    )

    assert response.status_code == 200
    assert response.json() == contract


@pytest.mark.asyncio
async def test_analysis_preserves_archived_launched_authority(monkeypatch):
    get_experiment = AsyncMock(
        return_value=make_experiment(
            {
                "status": "completed",
                "version": 8,
                "archived_at": "2026-09-01T00:00:00+00:00",
                "archived_by": "credential:archiver",
            }
        )
    )
    monkeypatch.setattr(experiments.pg_store, "get_experiment", get_experiment)

    response = await _get("/v1/experiments/checkout_exp/analysis")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["version"] == 8
    get_experiment.assert_awaited_once_with(
        app.state.pg_pool,
        "apdl",
        "checkout_exp",
    )


@pytest.mark.asyncio
async def test_analysis_returns_404_for_missing_tenant_record(monkeypatch):
    monkeypatch.setattr(
        experiments.pg_store,
        "get_experiment",
        AsyncMock(return_value=None),
    )

    response = await _get("/v1/experiments/missing/analysis")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "draft"},
        {"statistical_plan": None},
        {"variants_json": '[{"key":"control","weight":1}]'},
        {
            "variants_json": (
                '[{"key":"control","weight":1},'
                '{"key":"control","weight":1}]'
            )
        },
        {
            "variants_json": (
                '[{"key":"control","weight":1},'
                '{"key":"treatment","weight":0}]'
            )
        },
        {"default_variant": "missing"},
        {"primary_metric_json": "{}"},
        {"primary_metric_json": '{"event":"purchase","type":"revenue"}'},
        {"start_date": "2026-08-01T00:00:00"},
        {"end_date": "2026-07-31T00:00:00+00:00"},
        {
            "status": "stopped",
            "start_date": "2026-08-01T00:00:00+00:00",
            "end_date": None,
        },
        {"end_date": "2026-11-01T00:00:00+00:00"},
    ],
)
async def test_analysis_fails_closed_for_non_analyzable_stored_data(
    monkeypatch,
    overrides,
):
    monkeypatch.setattr(
        experiments.pg_store,
        "get_experiment",
        AsyncMock(return_value=make_experiment(overrides)),
    )

    response = await _get("/v1/experiments/checkout_exp/analysis")

    assert response.status_code == 409
    assert response.json()["error"] == "experiment_not_analyzable"


@pytest.mark.asyncio
async def test_analysis_requires_query_read(monkeypatch):
    async def authenticate_config_only(request: Request):
        principal = Principal(
            credential_id="config-only",
            project_id="apdl",
            roles=frozenset({"config:read", "config:write"}),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_config_only
    get_experiment = AsyncMock()
    monkeypatch.setattr(experiments.pg_store, "get_experiment", get_experiment)

    response = await _get("/v1/experiments/checkout_exp/analysis")

    assert response.status_code == 403
    assert response.json()["detail"] == "Credential requires role: query:read"
    get_experiment.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("parameter", ["project_id", "metric", "flag_key", "method"])
async def test_analysis_rejects_all_query_parameters(monkeypatch, parameter):
    get_experiment = AsyncMock()
    monkeypatch.setattr(experiments.pg_store, "get_experiment", get_experiment)

    response = await _get(
        f"/v1/experiments/checkout_exp/analysis?{parameter}=caller-controlled"
    )

    assert response.status_code == 422
    assert response.json()["detail"] == f"Unknown query parameter(s): {parameter}"
    get_experiment.assert_not_awaited()


@pytest.mark.asyncio
async def test_analysis_rejects_non_canonical_resource_key(monkeypatch):
    get_experiment = AsyncMock()
    monkeypatch.setattr(experiments.pg_store, "get_experiment", get_experiment)

    response = await _get("/v1/experiments/%20invalid/analysis")

    assert response.status_code == 422
    get_experiment.assert_not_awaited()
