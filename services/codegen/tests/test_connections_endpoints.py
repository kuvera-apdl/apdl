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


@pytest.mark.asyncio
async def test_repo_context_endpoint_serves_document(monkeypatch):
    import app.routers.connections as connections_router

    class _Token:
        token = "ghs_tok"

    async def fake_mint(installation_id, repo):
        assert repo == "acme/widgets"
        return _Token()

    async def fake_fetch(*, repo, branch, token):
        assert (repo, branch, token) == ("acme/widgets", "main", "ghs_tok")
        return {"repo": repo, "branch": branch, "framework": "Next.js (App Router)"}

    monkeypatch.setattr(connections_router, "mint_token_for_repo", fake_mint)
    monkeypatch.setattr(connections_router, "fetch_repo_context", fake_fetch)

    pool = FakePool()
    async with _client(pool) as client:
        await client.post(
            "/v1/connections",
            json={"project_id": "demo", "installation_id": 42, "repo": "acme/widgets"},
        )
        resp = await client.get("/v1/connections/demo/repo-context")

    assert resp.status_code == 200
    assert resp.json()["framework"] == "Next.js (App Router)"


@pytest.mark.asyncio
async def test_repo_context_endpoint_404_without_connection():
    async with _client(FakePool()) as client:
        resp = await client.get("/v1/connections/nope/repo-context")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_repo_context_endpoint_502_on_github_failure(monkeypatch):
    import httpx

    import app.routers.connections as connections_router

    async def fake_mint(installation_id, repo):
        raise httpx.ConnectError("github down")

    monkeypatch.setattr(connections_router, "mint_token_for_repo", fake_mint)

    pool = FakePool()
    async with _client(pool) as client:
        await client.post(
            "/v1/connections",
            json={"project_id": "demo", "installation_id": 42, "repo": "acme/widgets"},
        )
        resp = await client.get("/v1/connections/demo/repo-context")

    assert resp.status_code == 502
