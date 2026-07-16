"""Integration tests for query service router endpoints with mocked ClickHouse."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.config_client import ConfigExperimentAnalysis
from app.main import app
from app.models.schemas import ExperimentArmResult
from app.routers import experiments

PROJECT_ID = "apiasport"
PROJECT_API_KEY = "proj_apiasport_0123456789abcdef"
VARIANT_CONTEXT = {
    "default_variant": "control",
    "variants": [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ],
}
EXPERIMENT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "experiments"
    / "three-arm-analysis.json"
)


def _guardrail_request(flag_key: str, guardrail: dict) -> dict:
    return {
        "project_id": PROJECT_ID,
        "flag_key": flag_key,
        **VARIANT_CONTEXT,
        "guardrail": guardrail,
    }


@pytest.fixture(autouse=True)
def _setup_mock_ch():
    """Inject a mock ClickHouse client into app.state before each test."""
    mock_client = AsyncMock()
    mock_client.execute = AsyncMock(return_value=[])
    app.state.ch_client = mock_client
    auth_conn = AsyncMock()
    auth_conn.fetchval = AsyncMock(return_value=1)

    class Acquire:
        async def __aenter__(self):
            return auth_conn

        async def __aexit__(self, *exc):
            return False

    class AuthPool:
        def acquire(self):
            return Acquire()

    app.state.auth_pool = AuthPool()
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": PROJECT_API_KEY},
    ) as ac:
        yield ac


# ------------------------------------------------------------------
# Health & readiness
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "apdl-query"


@pytest.mark.asyncio
async def test_readiness_ok(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{"1": 1}])
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"


@pytest.mark.asyncio
async def test_readiness_fail(client):
    app.state.ch_client.execute = AsyncMock(side_effect=ConnectionError("down"))
    resp = await client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"


# ------------------------------------------------------------------
# Event endpoints
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_count(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"is_total": 0, "selector": "click", "event_name": "click", "event_count": 100, "unique_users": 50},
        {"is_total": 0, "selector": "view", "event_name": "view", "event_count": 200, "unique_users": 80},
        {"is_total": 1, "selector": "", "event_name": "", "event_count": 300, "unique_users": 100},
    ])

    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [
            {"event_name": "click", "filters": []},
            {"event_name": "view", "filters": []},
        ],
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_events"] == 300
    assert body["total_users"] == 100
    assert len(body["results"]) == 2


# ------------------------------------------------------------------
# Guardrail endpoints
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guardrail_frontend_error_count_trips_on_single_failure(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{
        "variant": "treatment",
        "default_variant": "control",
        "variant_sessions": 1,
        "default_sessions": 0,
        "variant_failure_sessions": 1,
        "default_failure_sessions": 0,
        "variant_failures": 1,
        "default_failures": 0,
    }])

    resp = await client.post(
        "/v1/query/guardrails/evaluate",
        json=_guardrail_request(
            "checkout-gate",
            {
                "metric": "frontend_error_count",
                "threshold": "at_least_one",
                "scope": "page:/checkout",
                "minimum_exposures": 0,
                "window_minutes": 10,
            },
        ),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tripped"] is True
    assert body["evidence"]["variant"] == "treatment"
    assert body["evidence"]["default_variant"] == "control"
    assert body["evidence"]["variant_failures"] == 1


@pytest.mark.asyncio
async def test_guardrail_frontend_error_rate_uses_baseline(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{
        "variant": "treatment",
        "default_variant": "control",
        "variant_sessions": 100,
        "default_sessions": 100,
        "variant_failure_sessions": 8,
        "default_failure_sessions": 2,
        "variant_failures": 8,
        "default_failures": 2,
    }])

    resp = await client.post(
        "/v1/query/guardrails/evaluate",
        json=_guardrail_request(
            "checkout-gate",
            {
                "metric": "frontend_error_rate",
                "threshold": "2x_baseline",
                "scope": "",
                "minimum_exposures": 100,
                "window_minutes": 10,
            },
        ),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tripped"] is True
    assert body["evidence"]["variant"] == "treatment"
    assert body["evidence"]["variant_error_rate"] == 0.08
    assert body["evidence"]["default_error_rate"] == 0.02


@pytest.mark.asyncio
async def test_guardrail_frontend_error_rate_trips_with_zero_baseline(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{
        "variant": "treatment",
        "default_variant": "control",
        "variant_sessions": 100,
        "default_sessions": 100,
        "variant_failure_sessions": 1,
        "default_failure_sessions": 0,
        "variant_failures": 1,
        "default_failures": 0,
    }])

    resp = await client.post(
        "/v1/query/guardrails/evaluate",
        json=_guardrail_request(
            "checkout-gate",
            {
                "metric": "frontend_error_rate",
                "threshold": "2x_baseline",
                "minimum_exposures": 100,
            },
        ),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["tripped"] is True
    assert body["evidence"]["variant_error_rate"] == 0.01
    assert body["evidence"]["default_error_rate"] == 0.0


@pytest.mark.asyncio
async def test_guardrail_frontend_error_rate_zero_baseline_no_exposed_failures(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{
        "variant": "treatment",
        "default_variant": "control",
        "variant_sessions": 100,
        "default_sessions": 100,
        "variant_failure_sessions": 0,
        "default_failure_sessions": 0,
        "variant_failures": 0,
        "default_failures": 0,
    }])

    resp = await client.post(
        "/v1/query/guardrails/evaluate",
        json=_guardrail_request(
            "checkout-gate",
            {
                "metric": "frontend_error_rate",
                "threshold": "2x_baseline",
                "minimum_exposures": 100,
            },
        ),
    )

    assert resp.status_code == 200
    assert resp.json()["tripped"] is False


@pytest.mark.asyncio
async def test_guardrail_rejects_noncanonical_fields(client):
    resp = await client.post(
        "/v1/query/guardrails/evaluate",
        json=_guardrail_request(
            "checkout-gate",
            {
                "metric": "frontend_error_count",
                "threshold": "at_least_one",
                "minimumExposures": 10,
            },
        ),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_guardrail_rejects_legacy_default_value_context(client):
    payload = _guardrail_request(
        "checkout-gate",
        {
            "metric": "frontend_error_count",
            "threshold": "at_least_one",
        },
    )
    payload["default_value"] = False

    resp = await client.post("/v1/query/guardrails/evaluate", json=payload)

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_guardrail_query_requires_active_flag_snapshot(client):
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.post(
        "/v1/query/guardrails/evaluate",
        json=_guardrail_request(
            "checkout-gate",
            {
                "metric": "frontend_error_count",
                "threshold": "at_least_one",
            },
        ),
    )

    assert resp.status_code == 200
    query = app.state.ch_client.execute.await_args.args[0]
    assert "min(exposure.first_exposure) AS exposure_time" in query
    assert "countIf(" in query
    assert "f.timestamp >= e.exposure_time" in query
    assert "count(f.session_id)" not in query
    assert "min(first_exposure) AS first_exposure" not in query
    assert "JSONHas(f.active_flags, %(flag_key)s)" in query
    assert "JSONExtractString(f.active_flags, %(flag_key)s) = e.variant" in query
    assert "JSONExtractBool(f.active_flags, %(flag_key)s)" not in query
    assert " value" not in query


@pytest.mark.asyncio
async def test_event_count_with_selector_filter(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {
            "is_total": 0,
            "selector": "$click[href eq /pricing]",
            "event_name": "$click",
            "event_count": 100,
            "unique_users": 50,
        },
        {
            "is_total": 1,
            "selector": "",
            "event_name": "",
            "event_count": 100,
            "unique_users": 50,
        },
    ])

    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [
            {
                "event_name": "$click",
                "filters": [{"property": "href", "operator": "eq", "value": "/pricing"}],
            }
        ],
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_events"] == 100
    query, params = app.state.ch_client.execute.await_args.args
    assert "JSONExtractString" in query
    assert params["count_0_filter_0_property"] == "href"
    assert params["count_0_filter_0_value"] == "/pricing"


@pytest.mark.asyncio
async def test_event_count_rejects_removed_event_names_field(client):
    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "event_names": ["click"],
    })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_event_count_denies_numeric_project_outside_authenticated_tenant(client):
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.post("/v1/query/events/count", json={
        "project_id": 1,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [{"event_name": "click", "filters": []}],
    })

    assert resp.status_code == 403
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_count_denies_cross_tenant_project(client):
    resp = await client.post("/v1/query/events/count", json={
        "project_id": "other",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [{"event_name": "click", "filters": []}],
    })

    assert resp.status_code == 403
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_requires_read_role(client):
    async def authenticate_without_query_role(request: Request):
        principal = Principal(
            credential_id="events-only",
            project_id=PROJECT_ID,
            roles=frozenset({"events:write"}),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_without_query_role

    resp = await client.post(
        "/v1/query/events/count",
        json={
            "project_id": PROJECT_ID,
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
            "selectors": [{"event_name": "click", "filters": []}],
        },
    )

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Credential requires role: query:read"
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_timeseries(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"bucket": "2025-01-01T00:00:00", "event_count": 10, "unique_users": 5},
        {"bucket": "2025-01-02T00:00:00", "event_count": 20, "unique_users": 8},
    ])

    resp = await client.post("/v1/query/events/timeseries", json={
        "project_id": PROJECT_ID,
        "selector": {
            "event_name": "click",
            "filters": [{"property": "country", "operator": "eq", "value": "US"}],
        },
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["selector"] == "click[country eq US]"
    assert len(body["buckets"]) == 2
    query, params = app.state.ch_client.execute.await_args.args
    assert "JSONExtractString" in query
    assert params["timeseries_filter_0_property"] == "country"


@pytest.mark.asyncio
async def test_event_breakdown(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {
            "selector": "click[page.path eq /pricing]",
            "property_type": "string",
            "property_value": "US",
            "event_count": 50,
            "unique_users": 20,
        },
        {
            "selector": "click[page.path eq /pricing]",
            "property_type": "string",
            "property_value": "UK",
            "event_count": 30,
            "unique_users": 15,
        },
    ])

    resp = await client.post("/v1/query/events/breakdown", json={
        "project_id": PROJECT_ID,
        "selector": {
            "event_name": "click",
            "filters": [{"property": "page.path", "operator": "eq", "value": "/pricing"}],
        },
        "property": "country",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["selector"] == "click[page.path eq /pricing]"
    assert body["property"] == "country"
    assert len(body["results"]) == 2
    assert body["results"][0] == {
        "selector": "click[page.path eq /pricing]",
        "property_type": "string",
        "property_value": "US",
        "event_count": 50,
        "unique_users": 20,
    }


@pytest.mark.asyncio
async def test_selector_rejects_invalid_operator(client):
    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [
            {
                "event_name": "$click",
                "filters": [{"property": "href", "operator": "starts_with", "value": "/"}],
            }
        ],
    })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_selector_rejects_malformed_selector(client):
    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [{"filters": []}],
    })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_selector_rejects_unsafe_property_name(client):
    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [
            {
                "event_name": "$click",
                "filters": [
                    {"property": "href'); DROP", "operator": "eq", "value": "/"}
                ],
            }
        ],
    })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_selector_rejects_unsupported_value_type(client):
    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "selectors": [
            {
                "event_name": "$click",
                "filters": [{"property": "href", "operator": "eq", "value": {"url": "/"}}],
            }
        ],
    })

    assert resp.status_code == 422


# ------------------------------------------------------------------
# Funnel endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funnel_analysis(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"step_number": 1, "users": 1000},
        {"step_number": 2, "users": 600},
        {"step_number": 3, "users": 200},
    ])

    resp = await client.post("/v1/query/funnel", json={
        "project_id": PROJECT_ID,
        "steps": [
            {"event_name": "view", "filters": []},
            {
                "event_name": "add_to_cart",
                "filters": [{"property": "sku", "operator": "exists"}],
            },
            {"event_name": "purchase", "filters": []},
        ],
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["steps"]) == 3
    assert body["steps"][0]["count"] == 1000
    assert body["steps"][0]["conversion_rate"] == 100.0
    assert body["steps"][1]["selector"] == "add_to_cart[sku exists]"
    assert body["steps"][2]["count"] == 200
    assert body["overall_conversion"] == 20.0
    query, params = app.state.ch_client.execute.await_args.args
    assert "windowFunnel" in query
    assert params["funnel_step_1_filter_0_property"] == "sku"


@pytest.mark.asyncio
async def test_funnel_too_few_steps(client):
    """A funnel with fewer than 2 steps is invalid."""
    resp = await client.post("/v1/query/funnel", json={
        "project_id": PROJECT_ID,
        "steps": [{"event_name": "only_one", "filters": []}],
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_funnel_no_data(client):
    """Funnel with no results from ClickHouse."""
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.post("/v1/query/funnel", json={
        "project_id": PROJECT_ID,
        "steps": [
            {"event_name": "a", "filters": []},
            {"event_name": "b", "filters": []},
        ],
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_conversion"] == 0.0


# ------------------------------------------------------------------
# Cohort endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cohort_comparison(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"cohort_value": "free", "day": "2025-01-01", "event_count": 100, "unique_users": 50, "total_users": 60},
        {"cohort_value": "free", "day": "2025-01-02", "event_count": 20, "unique_users": 40, "total_users": 60},
        {"cohort_value": "pro", "day": "2025-01-01", "event_count": 200, "unique_users": 80, "total_users": 80},
    ])

    resp = await client.post("/v1/query/cohort", json={
        "project_id": PROJECT_ID,
        "cohort_property": "plan",
        "metric_selector": {
            "event_name": "purchase",
            "filters": [{"property": "amount", "operator": "gte", "value": 50}],
        },
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["metric_selector"] == "purchase[amount gte 50]"
    assert body["cohort_property"] == "plan"
    assert len(body["cohorts"]) == 2
    assert body["cohorts"][0]["cohort_value"] == "free"
    assert body["cohorts"][0]["total_users"] == 60
    query, params = app.state.ch_client.execute.await_args.args
    assert "JSONExtractFloat" in query
    assert params["cohort_metric_filter_0_value"] == 50


# ------------------------------------------------------------------
# Retention endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_analysis(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"cohort_date": "2025-01-01", "cohort_size": 100, "period_offset": 0, "active_users": 100},
        {"cohort_date": "2025-01-01", "cohort_size": 100, "period_offset": 1, "active_users": 60},
        {"cohort_date": "2025-01-01", "cohort_size": 100, "period_offset": 2, "active_users": 40},
    ])

    resp = await client.post("/v1/query/retention", json={
        "project_id": PROJECT_ID,
        "cohort_selector": {
            "event_name": "signup",
            "filters": [{"property": "plan", "operator": "eq", "value": "pro"}],
        },
        "return_selector": {"event_name": "login", "filters": []},
        "cohort_mode": "first_match_in_window",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["cohort_mode"] == "first_match_in_window"
    assert body["cohort_selector"] == "signup[plan eq pro]"
    assert body["return_selector"] == "login"
    assert len(body["cohorts"]) == 1
    cohort = body["cohorts"][0]
    assert cohort["size"] == 100
    assert len(cohort["retention"]) == 3
    assert cohort["retention"][0] == 100.0
    assert cohort["retention"][1] == 60.0
    assert cohort["retention"][2] == 40.0


@pytest.mark.asyncio
async def test_retention_weekly(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"cohort_week": "2025-01-06", "cohort_size": 50, "period_offset": 0, "active_users": 50},
        {"cohort_week": "2025-01-06", "cohort_size": 50, "period_offset": 1, "active_users": 25},
    ])

    resp = await client.post("/v1/query/retention", json={
        "project_id": PROJECT_ID,
        "cohort_selector": {"event_name": "signup", "filters": []},
        "return_selector": {
            "event_name": "login",
            "filters": [{"property": "device_type", "operator": "neq", "value": "bot"}],
        },
        "cohort_mode": "first_match_in_window",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "period": "week",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["cohorts"]) == 1
    assert body["cohorts"][0]["retention"][1] == 50.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_update",
    [
        {},
        {"cohort_mode": "all_history"},
    ],
)
async def test_retention_requires_canonical_window_relative_mode(client, payload_update):
    payload = {
        "project_id": PROJECT_ID,
        "cohort_selector": {"event_name": "signup", "filters": []},
        "return_selector": {"event_name": "login", "filters": []},
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    }
    payload.update(payload_update)

    resp = await client.post("/v1/query/retention", json=payload)

    assert resp.status_code == 422


# ------------------------------------------------------------------
# Experiment endpoint
# ------------------------------------------------------------------


def _experiment_metadata(
    *,
    status: str = "completed",
    variants: list[str] | None = None,
    control_variant: str = "control",
    duration_days: int = 14,
    start_date: str | datetime | None = None,
    end_date: str | datetime | None = None,
    required_sample_size_per_arm: int = 20,
) -> ConfigExperimentAnalysis:
    start = start_date or datetime(2025, 1, 1, tzinfo=UTC)
    end = end_date or start + timedelta(days=duration_days)
    return ConfigExperimentAnalysis.model_validate(
        {
            "key": "exp_123",
            "flag_key": "checkout-experiment",
            "status": status,
            "control_variant": control_variant,
            "variants": variants or ["control", "treatment"],
            "metric_event": "purchase",
            "metric_direction": "increase",
            "statistical_plan": {
                "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
                "baseline_conversion_rate": 0.5,
                "minimum_detectable_effect": 0.5,
                "significance_level": 0.05,
                "nominal_power": 0.8,
                "required_sample_size_per_arm": required_sample_size_per_arm,
                "data_settlement_seconds": 5,
            },
            "start_date": start,
            "end_date": end,
            "version": 7,
        }
    )


@pytest.mark.asyncio
async def test_experiment_analyzes_every_declared_treatment(client, monkeypatch):
    metadata = _experiment_metadata(
        variants=["blue", "baseline", "green"],
        control_variant="baseline",
    )
    fetch = AsyncMock(return_value=metadata)
    monkeypatch.setattr(experiments, "fetch_experiment_analysis", fetch)
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "baseline",
                "sample_size": 100,
                "conversions": 10,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "blue",
                "sample_size": 100,
                "conversions": 20,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "green",
                "sample_size": 100,
                "conversions": 15,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_status"] == "decision_snapshot"
    assert body["experiment_key"] == "exp_123"
    assert body["flag_key"] == "checkout-experiment"
    assert body["metric_event"] == "purchase"
    assert [arm["variant"] for arm in body["arms"]] == [
        "blue",
        "baseline",
        "green",
    ]
    assert [item["treatment_variant"] for item in body["comparisons"]] == [
        "blue",
        "green",
    ]
    assert all(
        comparison["control_variant"] == "baseline"
        for comparison in body["comparisons"]
    )
    assert body["correction"] == "bonferroni"
    for comparison in body["comparisons"]:
        assert comparison["adjusted_p_value"] == pytest.approx(
            min(comparison["raw_p_value"] * 2, 1.0)
        )
    assert "recommendation" not in body
    assert body["deployment_readiness"] == "not_assessed"
    fetch.assert_awaited_once_with(PROJECT_ID, "exp_123", PROJECT_API_KEY)


@pytest.mark.asyncio
async def test_experiment_shared_three_arm_fixture_is_finite_and_complete(
    client,
    monkeypatch,
):
    fixture = json.loads(EXPERIMENT_FIXTURE_PATH.read_text())
    contract = ConfigExperimentAnalysis.model_validate(fixture["config_contract"])
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=contract),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=fixture["clickhouse_aggregates"]
    )

    response = await client.get(
        f"/v1/query/experiment/{contract.key}",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 200
    body = response.json()
    expected = fixture["expected"]
    assert body["analysis_status"] == expected["analysis_status"]
    assert [arm["variant"] for arm in body["arms"]] == expected["arm_order"]
    assert [
        comparison["treatment_variant"]
        for comparison in body["comparisons"]
    ] == expected["comparison_order"]
    assert body["crossover_actors"] == expected["crossover_actors"]
    assert body["unknown_variant_actors"] == expected["unknown_variant_actors"]
    assert body["identity_conflict_actors"] == expected["identity_conflict_actors"]
    assert body["identity_quality"] == "unambiguous"
    assert all(arm["conversion_rate"] == 0.0 for arm in body["arms"])
    assert all(
        comparison["raw_p_value"] == expected["raw_p_value"]
        and comparison["adjusted_p_value"] == expected["adjusted_p_value"]
        for comparison in body["comparisons"]
    )


@pytest.mark.asyncio
async def test_experiment_all_zero_conversions_are_finite(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata()),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 20,
                "conversions": 0,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "treatment",
                "sample_size": 20,
                "conversions": 0,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_status"] == "decision_snapshot"
    comparison = body["comparisons"][0]
    assert comparison["raw_p_value"] == 1.0
    assert comparison["adjusted_p_value"] == 1.0
    assert comparison["rate_difference"] == 0.0
    assert comparison["confidence_interval"][0] < 0.0
    assert comparison["confidence_interval"][1] > 0.0
    assert all(arm["conversion_rate"] == 0.0 for arm in body["arms"])


def test_sparse_perfect_split_uses_exact_test_and_non_wald_interval():
    result = experiments._comparison(
        ExperimentArmResult(
            variant="control",
            sample_size=2,
            conversions=0,
            conversion_rate=0.0,
        ),
        ExperimentArmResult(
            variant="treatment",
            sample_size=2,
            conversions=2,
            conversion_rate=1.0,
        ),
        comparison_count=1,
        significance_level=0.05,
    )

    assert result is not None
    assert result.raw_p_value == pytest.approx(1 / 3)
    assert result.confidence_interval != (1.0, 1.0)
    assert result.is_statistically_significant is False


@pytest.mark.asyncio
async def test_experiment_underpowered_declared_arm_is_typed(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata()),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 20,
                "conversions": 5,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "treatment",
                "sample_size": 1,
                "conversions": 1,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_status"] == "non_final"
    assert body["reason"] == "underpowered_arms"
    assert body["underpowered_variants"] == ["treatment"]
    assert body["statistical_plan"]["required_sample_size_per_arm"] == 20
    assert "comparisons" not in body


@pytest.mark.asyncio
async def test_experiment_zero_fills_missing_declared_arm(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(
            return_value=_experiment_metadata(
                variants=["control", "blue", "green"],
            )
        ),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 20,
                "conversions": 5,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "blue",
                "sample_size": 20,
                "conversions": 6,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_status"] == "non_final"
    assert body["underpowered_variants"] == ["green"]
    assert body["arms"][2] == {
        "variant": "green",
        "sample_size": 0,
        "conversions": 0,
        "conversion_rate": 0.0,
    }


@pytest.mark.asyncio
async def test_experiment_reports_crossovers_and_unknown_variants(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata()),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 20,
                "conversions": 1,
                "crossover_actors": 1,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "treatment",
                "sample_size": 20,
                "conversions": 2,
                "crossover_actors": 1,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "removed-arm",
                "sample_size": 4,
                "conversions": 4,
                "crossover_actors": 2,
                "identity_conflict_actors": 0,
            },
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["analysis_status"] == "decision_snapshot"
    assert body["crossover_actors"] == 4
    assert body["unknown_variant_actors"] == 4
    assert [arm["sample_size"] for arm in body["arms"]] == [20, 20]


@pytest.mark.asyncio
async def test_identity_alias_conflicts_prevent_decision_snapshot(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata()),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 20,
                "conversions": 5,
                "crossover_actors": 0,
                "identity_conflict_actors": 1,
            },
            {
                "variant": "treatment",
                "sample_size": 20,
                "conversions": 10,
                "crossover_actors": 0,
                "identity_conflict_actors": 1,
            },
        ]
    )

    response = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "non_final"
    assert body["reason"] == "identity_alias_conflicts"
    assert body["identity_conflict_actors"] == 1
    assert body["identity_quality"] == "degraded"
    assert "comparisons" not in body


@pytest.mark.asyncio
async def test_experiment_rejects_non_integer_clickhouse_aggregates(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata()),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 2.5,
                "conversions": 0,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            }
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "ClickHouse returned invalid experiment aggregates"


@pytest.mark.asyncio
async def test_experiment_uses_authoritative_metric_and_window(client, monkeypatch):
    metadata = _experiment_metadata(
        start_date="2025-01-01T01:00:00.123456+01:00",
        end_date="2025-01-31T01:00:00.654321+01:00",
    )
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=metadata),
    )
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    call = app.state.ch_client.execute.await_args
    assert "FROM feature_flag_exposures AS exposure FINAL" in call.args[0]
    assert call.args[1] == {
        "project_id": PROJECT_ID,
        "flag_key": metadata.flag_key,
        "metric_event": metadata.metric_event,
        "start_ms": 1_735_689_600_124,
        "end_ms": 1_738_281_600_655,
    }


@pytest.mark.asyncio
async def test_scheduled_experiment_does_not_query_clickhouse(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata(status="scheduled")),
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    assert resp.json()["reason"] == "experiment_not_started"
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [("running", "experiment_running"), ("stopped", "experiment_stopped")],
)
async def test_non_final_lifecycle_never_emits_comparisons(
    client,
    monkeypatch,
    status,
    reason,
):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata(status=status)),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 100,
                "conversions": 10,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "treatment",
                "sample_size": 100,
                "conversions": 30,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
        ]
    )

    response = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 200
    assert response.json()["analysis_status"] == "non_final"
    assert response.json()["reason"] == reason
    assert "comparisons" not in response.json()


@pytest.mark.asyncio
async def test_completed_experiment_before_declared_end_fails_closed(
    client,
    monkeypatch,
):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(
            return_value=_experiment_metadata(
                status="completed",
                start_date=now - timedelta(days=1),
                end_date=now + timedelta(days=1),
            )
        ),
    )

    response = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 200
    assert response.json()["analysis_status"] == "non_final"
    assert response.json()["reason"] == "experiment_window_open"
    assert "comparisons" not in response.json()
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_experiment_waits_for_predeclared_data_settlement(
    client,
    monkeypatch,
):
    now = datetime.now(UTC)
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(
            return_value=_experiment_metadata(
                status="completed",
                start_date=now - timedelta(days=1),
                end_date=now - timedelta(seconds=2),
            )
        ),
    )

    response = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert response.status_code == 200
    assert response.json()["analysis_status"] == "non_final"
    assert response.json()["reason"] == "awaiting_data_settlement"
    assert response.json()["data_completeness"] == "not_verified"
    assert "comparisons" not in response.json()
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_experiment_window_over_90_days_fails_closed(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata(duration_days=91)),
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 422
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_parameter", ["metric", "flag_key", "method"])
async def test_experiment_forbids_legacy_caller_parameters(
    client,
    monkeypatch,
    legacy_parameter,
):
    fetch = AsyncMock(return_value=_experiment_metadata())
    monkeypatch.setattr(experiments, "fetch_experiment_analysis", fetch)

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID, legacy_parameter: "caller-controlled"},
    )

    assert resp.status_code == 422
    fetch.assert_not_awaited()
    app.state.ch_client.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_experiment_preserves_exact_zero_p_value(client, monkeypatch):
    monkeypatch.setattr(
        experiments,
        "fetch_experiment_analysis",
        AsyncMock(return_value=_experiment_metadata()),
    )
    app.state.ch_client.execute = AsyncMock(
        return_value=[
            {
                "variant": "control",
                "sample_size": 1_000_000,
                "conversions": 0,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
            {
                "variant": "treatment",
                "sample_size": 1_000_000,
                "conversions": 1_000_000,
                "crossover_actors": 0,
                "identity_conflict_actors": 0,
            },
        ]
    )

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": PROJECT_ID},
    )

    assert resp.status_code == 200
    comparison = resp.json()["comparisons"][0]
    assert comparison["raw_p_value"] == 0.0
    assert comparison["adjusted_p_value"] == 0.0


@pytest.mark.asyncio
async def test_experiment_project_assertion_is_tenant_scoped(client, monkeypatch):
    fetch = AsyncMock(return_value=_experiment_metadata())
    monkeypatch.setattr(experiments, "fetch_experiment_analysis", fetch)

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={"project_id": "another-project"},
    )

    assert resp.status_code == 403
    fetch.assert_not_awaited()
