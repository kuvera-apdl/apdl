"""Independent liveness, core readiness, and capability-report contracts.

Uses ASGITransport so the FastAPI lifespan (which would require a live
Postgres) does not run.
"""

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app import main

app = main.app


class _ReadyConnection:
    async def fetchval(self, query):
        assert query == "SELECT 1"
        return 1


class _Pool:
    def __init__(self, error: Exception | None = None):
        self.error = error

    @asynccontextmanager
    async def acquire(self):
        if self.error is not None:
            raise self.error
        yield _ReadyConnection()


class _RunningTask:
    def done(self) -> bool:
        return False


@pytest.fixture
def runtime_state():
    original = dict(app.state._state)
    app.state.pg_pool = _Pool()
    app.state.authenticator = object()
    app.state.vector_store = object()
    app.state.run_dispatcher_task = _RunningTask()
    app.state.run_reaper_task = _RunningTask()
    app.state.approval_effect_task = _RunningTask()
    app.state.llm_reconciliation_task = _RunningTask()
    yield
    app.state._state.clear()
    app.state._state.update(original)


@pytest.mark.asyncio
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "apdl-agents"


@pytest.mark.asyncio
async def test_core_readiness_checks_runtime_and_postgres(runtime_state):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "apdl-agents",
        "checks": {"runtime": "ready", "postgres": "ready"},
    }


@pytest.mark.asyncio
async def test_core_readiness_fails_without_leaking_database_error(runtime_state):
    secret = "postgresql://user:secret@private/database"
    app.state.pg_pool = _Pool(RuntimeError(secret))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "runtime": "ready",
        "postgres": "not_ready",
    }
    assert secret not in response.text


@pytest.mark.asyncio
async def test_core_readiness_fails_when_runtime_is_incomplete(runtime_state):
    del app.state.vector_store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "runtime": "not_ready",
        "postgres": "ready",
    }


@pytest.mark.asyncio
async def test_degraded_capabilities_do_not_block_core_readiness(
    runtime_state, monkeypatch
):
    report = {
        "status": "degraded",
        "service": "apdl-agents",
        "capabilities": {
            "llm": {"configured": False, "reachable": False, "providers": {}},
            "query": {"configured": True, "reachable": False},
            "config": {"configured": True, "reachable": False},
            "codegen": {"configured": True, "reachable": False},
        },
    }

    async def fake_capability_report():
        return report

    monkeypatch.setattr(main, "capability_report", fake_capability_report)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        capability_response = await client.get("/ready/capabilities")
        core_response = await client.get("/ready")

    assert capability_response.status_code == 200
    assert capability_response.json() == report
    assert core_response.status_code == 200
    assert core_response.json()["status"] == "ready"
