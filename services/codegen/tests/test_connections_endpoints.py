"""Endpoint tests for read-only repository grants and tenant policy."""

from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.github.app_auth import (
    CODEGEN_READ_PERMISSIONS,
    AuthorizedRepositoryTarget,
    InstallationToken,
)
from app.github.token_broker import GitHubTokenBroker
from app.main import app
from tests.fakes import FakePool


async def _unexpected_issue(*args, **kwargs):
    raise AssertionError("token issuance was not expected")


async def _ignore_revoke(token: str) -> None:
    del token


def _client(
    pool: FakePool, *, token_broker: GitHubTokenBroker | None = None
) -> AsyncClient:
    app.state.pg_pool = pool
    app.state.github_token_broker = token_broker or GitHubTokenBroker(
        pool,
        issue_token=_unexpected_issue,
        revoke_token=_ignore_revoke,
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_get_connection_exposes_public_grant_identity_only():
    pool = FakePool()
    pool.add_connection(
        "demo",
        repo="acme/widgets",
        installation_id=42,
        repository_id=987,
        grant_id="ghg_demoactive",
    )

    async with _client(pool) as client:
        response = await client.get("/v1/connections/demo")

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == "demo"
    assert body["grant_id"] == "ghg_demoactive"
    assert body["repository_id"] == 987
    assert body["repository_full_name"] == "acme/widgets"
    assert body["default_base_branch"] == "main"
    assert body["tenant_policy"] == {
        "schema_version": "tenant_codegen_connection_policy@1",
        "test_cmd": None,
        "gates": {
            "max_files": None,
            "max_lines": None,
            "additional_protected_paths": [],
        },
        "runtime_acceptance": {
            "schema_version": "runtime_acceptance_request@1",
            "enabled": False,
        },
    }
    assert "installation_id" not in body
    assert "repo" not in body


@pytest.mark.asyncio
async def test_tenant_cannot_bind_repository():
    pool = FakePool()
    async with _client(pool) as client:
        response = await client.post(
            "/v1/connections",
            json={
                "project_id": "demo",
                "installation_id": 42,
                "repository_id": 987,
                "repository_full_name": "acme/widgets",
            },
        )

    assert response.status_code == 404
    assert pool.store["connections"] == {}


@pytest.mark.asyncio
async def test_tenant_cannot_disconnect_repository():
    pool = FakePool()
    pool.add_connection("demo")

    async with _client(pool) as client:
        response = await client.delete("/v1/connections/demo")
        still_connected = await client.get("/v1/connections/demo")

    assert response.status_code == 405
    assert still_connected.status_code == 200


@pytest.mark.asyncio
async def test_get_unknown_connection_404():
    async with _client(FakePool()) as client:
        response = await client.get("/v1/connections/demo")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_connection_read_and_policy_routes_reject_another_project(
    authorized_codegen_request,
):
    pool = FakePool()
    pool.add_connection("other")
    authorized_codegen_request("demo", frozenset({"agents:read", "agents:manage"}))

    async with _client(pool) as client:
        responses = [
            await client.get("/v1/connections/other"),
            await client.get("/v1/connections/other/tenant-policy"),
            await client.put("/v1/connections/other/tenant-policy", json={}),
            await client.get("/v1/connections/other/repo-context"),
        ]

    assert all(response.status_code == 403 for response in responses)


@pytest.mark.asyncio
async def test_get_and_replace_tenant_policy():
    pool = FakePool()
    pool.add_connection("demo")
    replacement = {
        "schema_version": "tenant_codegen_connection_policy@1",
        "test_cmd": "make ci",
        "gates": {
            "max_files": 5,
            "max_lines": 300,
            "additional_protected_paths": ["infra/**"],
        },
        "runtime_acceptance": {
            "schema_version": "runtime_acceptance_request@1",
            "enabled": True,
        },
    }

    async with _client(pool) as client:
        replaced = await client.put(
            "/v1/connections/demo/tenant-policy", json=replacement
        )
        got = await client.get("/v1/connections/demo/tenant-policy")

    assert replaced.status_code == 200
    assert replaced.json() == replacement
    assert got.status_code == 200
    assert got.json() == replacement


@pytest.mark.asyncio
async def test_replace_tenant_policy_is_strict_and_rejects_legacy_bypass_fields():
    pool = FakePool()
    pool.add_connection("demo")

    async with _client(pool) as client:
        response = await client.put(
            "/v1/connections/demo/tenant-policy",
            json={
                "schema_version": "tenant_codegen_connection_policy@1",
                "gates": {
                    "max_files": 10_000_000,
                    "max_lines": 10_000_000,
                    "protected_paths": [],
                    "allowed_protected_paths": [".env"],
                },
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_tenant_policy_routes_404_without_connection():
    async with _client(FakePool()) as client:
        got = await client.get("/v1/connections/demo/tenant-policy")
        replaced = await client.put("/v1/connections/demo/tenant-policy", json={})

    assert got.status_code == 404
    assert replaced.status_code == 404


@pytest.mark.asyncio
async def test_revoked_grant_hides_connection_and_tenant_policy():
    pool = FakePool()
    pool.add_connection("demo")
    grant_id = pool.store["connections"]["demo"]["grant_id"]
    pool.store["repository_grants"][grant_id]["status"] = "revoked"

    async with _client(pool) as client:
        connection = await client.get("/v1/connections/demo")
        policy = await client.get("/v1/connections/demo/tenant-policy")
        replacement = await client.put("/v1/connections/demo/tenant-policy", json={})

    assert connection.status_code == 404
    assert policy.status_code == 404
    assert replacement.status_code == 404


@pytest.mark.asyncio
async def test_tenant_policy_roles_are_read_and_manage(authorized_codegen_request):
    pool = FakePool()
    pool.add_connection("demo")
    authorized_codegen_request("demo", frozenset({"agents:read"}))

    async with _client(pool) as client:
        got = await client.get("/v1/connections/demo/tenant-policy")
        denied_write = await client.put("/v1/connections/demo/tenant-policy", json={})

    assert got.status_code == 200
    assert denied_write.status_code == 403


@pytest.mark.asyncio
async def test_repo_context_endpoint_uses_read_scoped_numeric_target(monkeypatch):
    import app.routers.connections as connections_router

    revoked: list[str] = []

    async def fake_issue(target, *, permissions):
        assert target == AuthorizedRepositoryTarget(
            installation_id=42,
            repository_id=987,
        )
        assert permissions == CODEGEN_READ_PERMISSIONS
        return InstallationToken(
            token="ghs_tok",
            expires_at=datetime(2026, 6, 17, 13, 0, tzinfo=timezone.utc),
        )

    async def fake_revoke(token: str) -> None:
        revoked.append(token)

    async def fake_fetch(*, repo, branch, token):
        assert (repo, branch, token) == ("acme/widgets", "main", "ghs_tok")
        from app.profiling.models import RepoProfile

        return RepoProfile(repo=repo, branch=branch, frameworks=["Next.js"])

    monkeypatch.setattr(connections_router, "fetch_repo_context", fake_fetch)

    pool = FakePool()
    pool.add_connection(
        "demo",
        repo="acme/widgets",
        installation_id=42,
        repository_id=987,
    )
    broker = GitHubTokenBroker(
        pool,
        issue_token=fake_issue,
        revoke_token=fake_revoke,
    )
    async with _client(pool, token_broker=broker) as client:
        response = await client.get("/v1/connections/demo/repo-context")

    assert response.status_code == 200
    assert response.json()["schema_version"] == "repo_profile@1"
    assert response.json()["frameworks"] == ["Next.js"]
    assert revoked == ["ghs_tok"]


@pytest.mark.asyncio
async def test_repo_context_endpoint_404_without_connection():
    async with _client(FakePool()) as client:
        response = await client.get("/v1/connections/demo/repo-context")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_repo_context_endpoint_502_on_github_failure():
    async def fake_issue(target, *, permissions):
        assert target == AuthorizedRepositoryTarget(
            installation_id=42,
            repository_id=987,
        )
        assert permissions == CODEGEN_READ_PERMISSIONS
        raise httpx.ConnectError("github down")

    pool = FakePool()
    pool.add_connection("demo", installation_id=42, repository_id=987)
    broker = GitHubTokenBroker(
        pool,
        issue_token=fake_issue,
        revoke_token=_ignore_revoke,
    )
    async with _client(pool, token_broker=broker) as client:
        response = await client.get("/v1/connections/demo/repo-context")

    assert response.status_code == 502
