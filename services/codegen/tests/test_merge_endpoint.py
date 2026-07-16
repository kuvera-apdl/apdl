"""APDL must not expose a pull-request merge endpoint."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.fakes import FakePool


@pytest.mark.asyncio
async def test_merge_endpoint_does_not_exist():
    app.state.pg_pool = FakePool()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/changesets/cs_any/merge", json={})
    assert response.status_code == 404
