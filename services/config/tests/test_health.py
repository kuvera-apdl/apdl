"""Independent Config liveness and dependency readiness contracts."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


class ReadyConnection:
    async def fetchval(self, sql):
        assert sql == "SELECT 1"
        return 1


class Pool:
    def __init__(self, error: Exception | None = None):
        self.error = error

    @asynccontextmanager
    async def acquire(self):
        if self.error is not None:
            raise self.error
        yield ReadyConnection()


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
async def test_readiness_returns_200_when_dependencies_are_ready(health_state):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"] == {"postgres": "ready", "redis": "ready"}
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
