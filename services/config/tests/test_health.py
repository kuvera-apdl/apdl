"""Independent Config liveness and dependency readiness contracts."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app import main as config_main
from app import outbox
from app.main import app


class ReadyConnection:
    def __init__(self, metrics: dict | None = None):
        self.metrics = metrics or outbox.empty_metrics()

    async def fetchval(self, sql):
        assert sql == "SELECT 1"
        return 1

    async def fetchrow(self, sql):
        assert "FROM config_outbox" in sql
        return self.metrics


class Pool:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        metrics: dict | None = None,
    ):
        self.error = error
        self.metrics = metrics

    @asynccontextmanager
    async def acquire(self):
        if self.error is not None:
            raise self.error
        yield ReadyConnection(self.metrics)


@pytest.fixture
def health_state():
    original = dict(app.state._state)
    app.state.pg_pool = Pool()
    app.state.redis = AsyncMock()
    app.state.redis.ping.return_value = True
    app.state.broadcaster = AsyncMock()
    app.state.broadcaster.metrics_snapshot.return_value = {
        "active_connections": 0,
        "accepted_total": 0,
        "rejected_total": {
            "global": 0,
            "project": 0,
            "credential": 0,
            "ip": 0,
        },
        "closed_total": {},
        "queue_overflow_total": 0,
    }
    yield
    app.state._state.clear()
    app.state._state.update(original)


@pytest.mark.asyncio
async def test_liveness_does_not_touch_dependencies(health_state):
    app.state.pg_pool = Pool(RuntimeError("postgres secret"))
    app.state.redis.ping.side_effect = RuntimeError("redis secret")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "apdl-config"}
    app.state.redis.ping.assert_not_awaited()


@pytest.mark.asyncio
async def test_experiment_analysis_capability_is_exact_and_schema_backed(
    health_state,
    monkeypatch,
):
    schema_probe = AsyncMock()
    monkeypatch.setattr(config_main, "assert_schema_ready", schema_probe)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready/experiment-analysis")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "apdl-config",
        "capability": "experiment_analysis",
        "schema_version": "config_experiment_analysis@1",
    }
    schema_probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_experiment_analysis_capability_fails_closed_without_leaking(
    health_state,
    monkeypatch,
):
    secret = "postgresql://user:secret@private/database"
    monkeypatch.setattr(
        config_main,
        "assert_schema_ready",
        AsyncMock(side_effect=RuntimeError(secret)),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready/experiment-analysis")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "service": "apdl-config",
        "capability": "experiment_analysis",
        "schema_version": "config_experiment_analysis@1",
    }
    assert secret not in response.text


@pytest.mark.asyncio
async def test_readiness_returns_200_when_dependencies_are_ready(health_state):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"] == {
        "postgres": "ready",
        "redis": "ready",
        "outbox": "ready",
    }
    assert payload["outbox"]["pending_count"] == 0
    assert payload["outbox"]["estimated_receipt_count"] == 0
    assert payload["outbox"]["thresholds"][
        "exposure_receipt_retention_seconds"
    ] == outbox.EXPOSURE_RECEIPT_RETENTION_SECONDS
    assert payload["outbox"]["status"] == "ready"
    assert payload["sse"]["active_connections"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["postgres", "redis"])
async def test_readiness_returns_503_without_leaking_dependency_errors(
    dependency,
    health_state,
):
    secret = "postgresql://user:secret@private/database"
    if dependency == "postgres":
        app.state.pg_pool = Pool(RuntimeError(secret))
    else:
        app.state.redis.ping.side_effect = RuntimeError(secret)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"][dependency] == "not_ready"
    assert secret not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metrics_update", "reason"),
    [
        (
            {
                "oldest_pending_age_seconds": (
                    outbox.READINESS_MAX_PENDING_AGE_SECONDS + 1
                )
            },
            "oldest_pending_age_exceeded",
        ),
        (
            {"quarantined_count": outbox.READINESS_MAX_QUARANTINED_ROWS + 1},
            "quarantined_rows_exceeded",
        ),
    ],
)
async def test_readiness_degrades_when_outbox_crosses_threshold(
    health_state,
    metrics_update,
    reason,
):
    metrics = {**outbox.empty_metrics(), **metrics_update}
    app.state.pg_pool = Pool(metrics=metrics)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["postgres"] == "ready"
    assert payload["checks"]["outbox"] == "degraded"
    assert payload["outbox"]["degraded_reasons"] == [reason]
