"""Integration tests for query service router endpoints with mocked ClickHouse."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app

PROJECT_ID = "apiasport"


@pytest.fixture(autouse=True)
def _setup_mock_ch():
    """Inject a mock ClickHouse client into app.state before each test."""
    mock_client = AsyncMock()
    mock_client.execute = AsyncMock(return_value=[])
    app.state.ch_client = mock_client
    yield


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_ready"


# ------------------------------------------------------------------
# Event endpoints
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_count(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"event_name": "click", "event_count": 100, "unique_users": 50},
        {"event_name": "view", "event_count": 200, "unique_users": 80},
    ])

    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_events"] == 300
    assert body["total_users"] == 130
    assert len(body["results"]) == 2


# ------------------------------------------------------------------
# Guardrail endpoints
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guardrail_frontend_error_count_trips_on_single_failure(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{
        "exposed_sessions": 1,
        "baseline_sessions": 0,
        "exposed_failure_sessions": 1,
        "baseline_failure_sessions": 0,
        "exposed_failures": 1,
        "baseline_failures": 0,
    }])

    resp = await client.post("/v1/query/guardrails/evaluate", json={
        "project_id": PROJECT_ID,
        "flag_key": "checkout-gate",
        "guardrail": {
            "metric": "frontend_error_count",
            "threshold": "at_least_one",
            "scope": "page:/checkout",
            "minimum_exposures": 0,
            "window_minutes": 10,
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["tripped"] is True
    assert body["evidence"]["exposed_failures"] == 1


@pytest.mark.asyncio
async def test_guardrail_frontend_error_rate_uses_baseline(client):
    app.state.ch_client.execute = AsyncMock(return_value=[{
        "exposed_sessions": 100,
        "baseline_sessions": 100,
        "exposed_failure_sessions": 8,
        "baseline_failure_sessions": 2,
        "exposed_failures": 8,
        "baseline_failures": 2,
    }])

    resp = await client.post("/v1/query/guardrails/evaluate", json={
        "project_id": PROJECT_ID,
        "flag_key": "checkout-gate",
        "guardrail": {
            "metric": "frontend_error_rate",
            "threshold": "2x_baseline",
            "scope": "",
            "minimum_exposures": 100,
            "window_minutes": 10,
        },
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["tripped"] is True
    assert body["evidence"]["exposed_error_rate"] == 0.08
    assert body["evidence"]["baseline_error_rate"] == 0.02


@pytest.mark.asyncio
async def test_guardrail_rejects_noncanonical_fields(client):
    resp = await client.post("/v1/query/guardrails/evaluate", json={
        "project_id": PROJECT_ID,
        "flag_key": "checkout-gate",
        "guardrail": {
            "metric": "frontend_error_count",
            "threshold": "at_least_one",
            "minimumExposures": 10,
        },
    })

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_guardrail_query_requires_active_flag_snapshot(client):
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.post("/v1/query/guardrails/evaluate", json={
        "project_id": PROJECT_ID,
        "flag_key": "checkout-gate",
        "guardrail": {
            "metric": "frontend_error_count",
            "threshold": "at_least_one",
        },
    })

    assert resp.status_code == 200
    query = app.state.ch_client.execute.await_args.args[0]
    assert "JSONHas(f.active_flags, %(flag_key)s)" in query
    assert "JSONExtractBool(f.active_flags, %(flag_key)s)" in query


@pytest.mark.asyncio
async def test_event_count_with_filter(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"event_name": "click", "event_count": 100, "unique_users": 50},
    ])

    resp = await client.post("/v1/query/events/count", json={
        "project_id": PROJECT_ID,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "event_names": ["click"],
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_events"] == 100


@pytest.mark.asyncio
async def test_event_count_coerces_legacy_numeric_project_id(client):
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.post("/v1/query/events/count", json={
        "project_id": 1,
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    _, params = app.state.ch_client.execute.await_args.args
    assert params["project_id"] == "1"


@pytest.mark.asyncio
async def test_event_timeseries(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"bucket": "2025-01-01T00:00:00", "event_count": 10, "unique_users": 5},
        {"bucket": "2025-01-02T00:00:00", "event_count": 20, "unique_users": 8},
    ])

    resp = await client.post("/v1/query/events/timeseries", json={
        "project_id": PROJECT_ID,
        "event_name": "click",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["buckets"]) == 2


@pytest.mark.asyncio
async def test_event_breakdown(client):
    app.state.ch_client.execute = AsyncMock(return_value=[
        {"property_value": "US", "event_count": 50, "unique_users": 20},
        {"property_value": "UK", "event_count": 30, "unique_users": 15},
    ])

    resp = await client.post("/v1/query/events/breakdown", json={
        "project_id": PROJECT_ID,
        "event_name": "click",
        "property": "country",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2


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
        "steps": ["view", "add_to_cart", "purchase"],
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["steps"]) == 3
    assert body["steps"][0]["count"] == 1000
    assert body["steps"][0]["conversion_rate"] == 100.0
    assert body["steps"][2]["count"] == 200
    assert body["overall_conversion"] == 20.0


@pytest.mark.asyncio
async def test_funnel_too_few_steps(client):
    """A funnel with fewer than 2 steps returns empty."""
    resp = await client.post("/v1/query/funnel", json={
        "project_id": PROJECT_ID,
        "steps": ["only_one"],
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert body["steps"] == []
    assert body["overall_conversion"] == 0.0


@pytest.mark.asyncio
async def test_funnel_no_data(client):
    """Funnel with no results from ClickHouse."""
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.post("/v1/query/funnel", json={
        "project_id": PROJECT_ID,
        "steps": ["a", "b"],
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
        {"cohort_value": "free", "day": "2025-01-01", "event_count": 100, "unique_users": 50},
        {"cohort_value": "pro", "day": "2025-01-01", "event_count": 200, "unique_users": 80},
    ])

    resp = await client.post("/v1/query/cohort", json={
        "project_id": PROJECT_ID,
        "cohort_property": "plan",
        "metric_event": "purchase",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["cohorts"]) == 2


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
        "cohort_event": "signup",
        "return_event": "login",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    })

    assert resp.status_code == 200
    body = resp.json()
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
        "cohort_event": "signup",
        "return_event": "login",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "period": "week",
    })

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["cohorts"]) == 1
    assert body["cohorts"][0]["retention"][1] == 50.0


# ------------------------------------------------------------------
# Experiment endpoint
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_experiment_results_frequentist(client):
    """Experiment endpoint with mocked metric/exposure data."""
    # First call: metric rows, second call: exposure rows
    app.state.ch_client.execute = AsyncMock(side_effect=[
        # EXPERIMENT_METRICS_QUERY result
        [
            {"variant": "control", "user_id": f"u{i}", "metric_value": 1} for i in range(50)
        ] + [
            {"variant": "treatment", "user_id": f"t{i}", "metric_value": 2} for i in range(50)
        ],
        # EXPERIMENT_EXPOSURES_QUERY result
        [
            {"variant": "control", "user_id": f"u{i}"} for i in range(50)
        ] + [
            {"variant": "treatment", "user_id": f"t{i}"} for i in range(50)
        ],
    ])

    resp = await client.get(
        "/v1/query/experiment/exp_123",
        params={
            "metric": "purchase",
            "method": "frequentist",
            "project_id": PROJECT_ID,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["experiment_id"] == "exp_123"
    assert body["metric"] == "purchase"
    assert body["method"] == "frequentist"
    assert len(body["variants"]) == 2
    assert body["is_significant"] is True


@pytest.mark.asyncio
async def test_experiment_no_data_returns_404(client):
    """When no metric data exists, return 404."""
    app.state.ch_client.execute = AsyncMock(return_value=[])

    resp = await client.get(
        "/v1/query/experiment/exp_missing",
        params={"metric": "purchase", "project_id": PROJECT_ID},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_experiment_single_variant_returns_400(client):
    """Experiment with only one variant should return 400."""
    app.state.ch_client.execute = AsyncMock(side_effect=[
        # metric rows - only control
        [{"variant": "control", "user_id": f"u{i}", "metric_value": 1} for i in range(10)],
        # exposure rows - only control
        [{"variant": "control", "user_id": f"u{i}"} for i in range(10)],
    ])

    resp = await client.get(
        "/v1/query/experiment/exp_single",
        params={"metric": "purchase", "project_id": PROJECT_ID},
    )

    assert resp.status_code == 400
