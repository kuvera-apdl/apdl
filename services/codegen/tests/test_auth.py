"""Internal-token guard behavior."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.fakes import FakePool


@pytest.mark.asyncio
async def test_internal_token_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "s3cret")
    app.state.pg_pool = FakePool()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing token → 401 before the handler runs.
        missing = await client.get("/v1/connections/demo")
        assert missing.status_code == 401

        # Correct token → passes auth (then 404 because no such connection).
        ok = await client.get(
            "/v1/connections/demo", headers={"X-APDL-Internal-Token": "s3cret"}
        )
        assert ok.status_code == 404


@pytest.mark.asyncio
async def test_no_token_configured_is_permissive(monkeypatch):
    monkeypatch.delenv("APDL_INTERNAL_TOKEN", raising=False)
    app.state.pg_pool = FakePool()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/connections/demo")
    # Permissive in local dev: auth passes, handler returns 404 for unknown project.
    assert resp.status_code == 404
