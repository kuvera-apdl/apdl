"""Endpoint tests for the repo connection registry."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.fakes import FakePool


def _client(pool: FakePool) -> AsyncClient:
    app.state.pg_pool = pool
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_and_get_connection():
    pool = FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/connections",
            json={"project_id": "demo", "installation_id": 42, "repo": "acme/widgets"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["project_id"] == "demo"
        assert body["repo"] == "acme/widgets"
        assert body["default_base_branch"] == "main"

        got = await client.get("/v1/connections/demo")
        assert got.status_code == 200
        assert got.json()["installation_id"] == 42


@pytest.mark.asyncio
async def test_get_unknown_connection_404():
    async with _client(FakePool()) as client:
        resp = await client.get("/v1/connections/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_connection_rejects_bad_repo():
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/connections",
            json={"project_id": "demo", "installation_id": 1, "repo": "not-a-repo"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_connection_rejects_unknown_field():
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/connections",
            json={
                "project_id": "demo",
                "installation_id": 1,
                "repo": "acme/widgets",
                "token": "x",
            },
        )
    assert resp.status_code == 422
