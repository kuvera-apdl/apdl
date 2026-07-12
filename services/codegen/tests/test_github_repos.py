"""Tests for the project-scoped repo view (``GET /v1/github/repos``).

The endpoint is exercised through a monkeypatched listing seam; the listing
itself (installation walk + pagination) runs against an httpx MockTransport.
"""

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.github.installations import AccessibleRepo, list_accessible_repos
from app.main import app
from tests.fakes import FakePool


def _client(pool: FakePool) -> AsyncClient:
    app.state.pg_pool = pool
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_repos_endpoint_serves_listing(monkeypatch):
    import app.routers.github as github_router

    async def fake_list():
        return [
            AccessibleRepo(
                repo="acme/widgets",
                installation_id=42,
                account="acme",
                default_branch="main",
                private=True,
            ),
            AccessibleRepo(
                repo="other/secret",
                installation_id=99,
                account="other",
                default_branch="main",
                private=True,
            ),
        ]

    monkeypatch.setattr(github_router, "list_accessible_repos", fake_list)
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    async with _client(pool) as client:
        resp = await client.get("/v1/github/repos", params={"project_id": "demo"})
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "repo": "acme/widgets",
            "installation_id": 42,
            "account": "acme",
            "default_branch": "main",
            "private": True,
        }
    ]


@pytest.mark.asyncio
async def test_repos_endpoint_rejects_another_project(authorized_codegen_request):
    authorized_codegen_request("demo", frozenset({"agents:read"}))
    async with _client(FakePool()) as client:
        response = await client.get(
            "/v1/github/repos", params={"project_id": "other"}
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_repos_endpoint_503_without_credentials(monkeypatch):
    import app.routers.github as github_router

    async def fake_list():
        raise ValueError("GitHub App ID and private key are required to mint a JWT.")

    monkeypatch.setattr(github_router, "list_accessible_repos", fake_list)
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    async with _client(pool) as client:
        resp = await client.get("/v1/github/repos", params={"project_id": "demo"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_repos_endpoint_502_on_github_failure(monkeypatch):
    import app.routers.github as github_router

    async def fake_list():
        raise httpx.ConnectError("github down")

    monkeypatch.setattr(github_router, "list_accessible_repos", fake_list)
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    async with _client(pool) as client:
        resp = await client.get("/v1/github/repos", params={"project_id": "demo"})
    assert resp.status_code == 502


# --- list_accessible_repos against a mocked GitHub API ----------------------


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _repo(full_name: str, *, default_branch: str = "main", private: bool = False) -> dict:
    return {"full_name": full_name, "default_branch": default_branch, "private": private}


@pytest.mark.asyncio
async def test_list_accessible_repos_walks_installations_and_paginates():
    # Two installations; installation 2's repo grant spans two pages (100 + 1).
    # GitHub scopes /installation/repositories by token, so the fake token
    # carries the installation id and the handler routes fixtures off it.
    inst2_page1 = [_repo(f"beta/r{i:03d}") for i in range(100)]
    inst2_page2 = [_repo("beta/zeta", default_branch="develop", private=True)]

    def handle(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if request.url.path == "/app/installations":
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "account": {"login": "acme"}},
                    {"id": 2, "account": {"login": "beta"}},
                ],
            )
        if request.url.path.endswith("/access_tokens"):
            inst = request.url.path.split("/")[3]
            return httpx.Response(
                201, json={"token": f"ghs_{inst}", "expires_at": "2026-07-03T12:00:00Z"}
            )
        if request.url.path == "/installation/repositories":
            token = request.headers["Authorization"].removeprefix("Bearer ghs_")
            if token == "1":
                repositories = [_repo("acme/widgets", private=True)] if page == 1 else []
            else:
                repositories = {1: inst2_page1, 2: inst2_page2}.get(page, [])
            return httpx.Response(200, json={"repositories": repositories})
        raise AssertionError(f"unexpected path {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    repos = await list_accessible_repos(
        app_id="123456", private_key_pem=_pem(), client=client
    )
    await client.aclose()

    # 1 repo from installation 1 + (100 + 1) from installation 2's two pages.
    assert len(repos) == 102
    assert repos == sorted(repos, key=lambda r: r.repo.lower())

    widgets = next(r for r in repos if r.repo == "acme/widgets")
    assert (widgets.installation_id, widgets.account) == (1, "acme")
    assert widgets.private is True

    zeta = next(r for r in repos if r.repo == "beta/zeta")
    assert (zeta.installation_id, zeta.account) == (2, "beta")
    assert zeta.default_branch == "develop"


@pytest.mark.asyncio
async def test_list_accessible_repos_empty_when_no_installations():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected path {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    repos = await list_accessible_repos(
        app_id="123456", private_key_pem=_pem(), client=client
    )
    await client.aclose()
    assert repos == []


@pytest.mark.asyncio
async def test_list_accessible_repos_requires_credentials():
    with pytest.raises(ValueError):
        await list_accessible_repos(app_id="", private_key_pem="")
