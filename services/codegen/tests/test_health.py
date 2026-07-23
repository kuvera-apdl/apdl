"""Smoke test for the codegen service.

Uses ASGITransport so the FastAPI lifespan (which would require a live
PostgreSQL) does not run. /health does not touch any shared resources.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.evaluations.models import RolloutStage
from app.main import app


@pytest.mark.asyncio
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "apdl-codegen"


@pytest.mark.asyncio
async def test_ready_returns_200_when_db_reachable():
    from tests.fakes import FakePool

    app.state.pg_pool = FakePool()
    app.state.codegen_rollout_stage = RolloutStage.offline
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ready",
        "service": "apdl-codegen",
        "capabilities": {"changeset_creation": "disabled"},
    }


@pytest.mark.asyncio
async def test_ready_requires_a_tenant_scoped_check_for_publication_stages():
    from tests.fakes import FakePool

    app.state.pg_pool = FakePool()
    app.state.codegen_rollout_stage = RolloutStage.development_pr
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")

    assert resp.status_code == 200
    assert resp.json()["capabilities"] == {"changeset_creation": "tenant_scoped"}


@pytest.mark.asyncio
async def test_ready_returns_503_when_db_unreachable():
    # Orchestrators key on the status code — not-ready must not be a 200.
    class _BrokenPool:
        def acquire(self):
            raise RuntimeError("no database")

    app.state.pg_pool = _BrokenPool()
    app.state.codegen_rollout_stage = RolloutStage.reviewed_pr
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
    assert resp.json()["capabilities"] == {"changeset_creation": "tenant_scoped"}
