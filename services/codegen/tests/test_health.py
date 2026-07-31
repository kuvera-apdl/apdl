"""Smoke test for the codegen service.

Uses ASGITransport so the FastAPI lifespan (which would require a live
PostgreSQL) does not run. /health does not touch any shared resources.
"""

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from app.main import app
from app.models.execution import PublicationStage
from app.store.llm_credentials import ProjectCredentialStore


def _credential_store(pool: object) -> ProjectCredentialStore:
    del pool

    async def unavailable(_request: Request) -> Response:
        return Response(503)

    return ProjectCredentialStore(
        AsyncClient(transport=MockTransport(unavailable)),
        base_url="http://vault.test",
        token="v" * 32,
    )


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

    pool = FakePool()
    app.state.pg_pool = pool
    app.state.llm_credential_store = _credential_store(pool)
    app.state.codegen_rollout_stage = PublicationStage.offline
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ready",
        "service": "apdl-codegen",
        "capabilities": {
            "changeset_creation": "disabled",
            "credential_store": "ready",
        },
    }


@pytest.mark.asyncio
async def test_ready_requires_a_tenant_scoped_check_for_publication_stages():
    from tests.fakes import FakePool

    pool = FakePool()
    app.state.pg_pool = pool
    app.state.llm_credential_store = _credential_store(pool)
    app.state.codegen_rollout_stage = PublicationStage.development_pr
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")

    assert resp.status_code == 200
    assert resp.json()["capabilities"] == {
        "changeset_creation": "tenant_scoped",
        "credential_store": "ready",
    }


@pytest.mark.asyncio
async def test_ready_returns_503_when_db_unreachable():
    # Orchestrators key on the status code — not-ready must not be a 200.
    class _BrokenPool:
        def acquire(self):
            raise RuntimeError("no database")

    app.state.pg_pool = _BrokenPool()
    app.state.llm_credential_store = _credential_store(app.state.pg_pool)
    app.state.codegen_rollout_stage = PublicationStage.tenant_draft_pr
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "not_ready"
    assert resp.json()["capabilities"] == {
        "changeset_creation": "tenant_scoped",
        "credential_store": "blocked",
    }
