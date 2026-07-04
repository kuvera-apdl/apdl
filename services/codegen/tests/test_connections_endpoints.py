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
async def test_delete_connection():
    pool = FakePool()
    pool.add_connection("demo")
    async with _client(pool) as client:
        resp = await client.delete("/v1/connections/demo")
        assert resp.status_code == 204

        got = await client.get("/v1/connections/demo")
        assert got.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_connection_404():
    async with _client(FakePool()) as client:
        resp = await client.delete("/v1/connections/nope")
    assert resp.status_code == 404


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
async def test_create_connection_resolves_omitted_installation_id(monkeypatch):
    import app.routers.connections as connections_router

    async def fake_resolve(repo):
        assert repo == "acme/widgets"
        return 987

    monkeypatch.setattr(connections_router, "resolve_installation_id", fake_resolve)
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/connections",
            json={"project_id": "demo", "repo": "acme/widgets"},
        )
    assert resp.status_code == 201
    assert resp.json()["installation_id"] == 987


@pytest.mark.asyncio
async def test_create_connection_422_when_app_not_installed(monkeypatch):
    import httpx

    import app.routers.connections as connections_router

    async def fake_resolve(repo):
        request = httpx.Request("GET", "https://api.github.com/repos/x/installation")
        raise httpx.HTTPStatusError(
            "404", request=request, response=httpx.Response(404, request=request)
        )

    monkeypatch.setattr(connections_router, "resolve_installation_id", fake_resolve)
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/connections",
            json={"project_id": "demo", "repo": "acme/uninstalled"},
        )
    assert resp.status_code == 422
    assert "not installed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_connection_503_without_app_credentials(monkeypatch):
    import app.routers.connections as connections_router

    async def fake_resolve(repo):
        raise ValueError("GitHub App ID and private key are required to mint a JWT.")

    monkeypatch.setattr(connections_router, "resolve_installation_id", fake_resolve)
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/connections",
            json={"project_id": "demo", "repo": "acme/widgets"},
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_create_connection_explicit_id_skips_resolution(monkeypatch):
    import app.routers.connections as connections_router

    async def explode(repo):  # pragma: no cover - must not be called
        raise AssertionError("resolution must be skipped when the id is explicit")

    monkeypatch.setattr(connections_router, "resolve_installation_id", explode)
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/connections",
            json={"project_id": "demo", "installation_id": 42, "repo": "acme/widgets"},
        )
    assert resp.status_code == 201
    assert resp.json()["installation_id"] == 42


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
