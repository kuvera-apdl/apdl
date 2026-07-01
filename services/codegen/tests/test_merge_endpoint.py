"""Endpoint tests for changeset merge (green-CI enforced, GitHub mocked)."""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.github.app_auth import InstallationToken
from app.github.pulls import MergeResult
from app.main import app
from app.routers import changesets
from tests.fakes import FakePool


def _client(pool: FakePool) -> AsyncClient:
    app.state.pg_pool = pool
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _patch_merge(monkeypatch, *, merged: bool = True) -> dict:
    calls: dict = {}

    async def fake_mint(installation_id: int, repo: str):
        calls["installation_id"] = installation_id
        calls["repo"] = repo
        return InstallationToken(
            token="ghs_tok", expires_at=datetime(2026, 6, 17, tzinfo=timezone.utc)
        )

    async def fake_merge(**kwargs):
        calls["merge"] = kwargs
        return MergeResult(merged=merged, sha="abc123")

    monkeypatch.setattr(changesets, "mint_token_for_repo", fake_mint)
    monkeypatch.setattr(changesets, "merge_pull_request", fake_merge)
    return calls


@pytest.mark.asyncio
async def test_merge_succeeds_when_ci_green(monkeypatch):
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=7)
    pool.add_changeset(
        "cs_m1", "demo", status="ci_passed", ci_status="passed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_m1/merge", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"
    assert calls["merge"]["repo"] == "acme/widgets"
    assert calls["merge"]["number"] == 5
    assert calls["installation_id"] == 7


@pytest.mark.asyncio
async def test_merge_succeeds_when_repo_has_no_ci(monkeypatch):
    # ci_status="none" means the repo has no CI to gate on — merge is allowed.
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=7)
    pool.add_changeset(
        "cs_none", "demo", status="ci_passed", ci_status="none", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_none/merge", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"
    assert calls["merge"]["number"] == 5


@pytest.mark.asyncio
async def test_merge_refused_when_ci_pending(monkeypatch):
    # "pending" must still block — only "passed"/"none" clear the gate.
    _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_mp", "demo", status="ci_passed", ci_status="pending", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_mp/merge", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_merge_refused_without_green_ci(monkeypatch):
    _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_m2", "demo", status="ci_failed", ci_status="failed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_m2/merge", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_merge_refused_in_non_mergeable_state(monkeypatch):
    _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_m3", "demo", status="pr_open", ci_status="passed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_m3/merge", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_merge_409_when_github_declines(monkeypatch):
    # GitHub refusing the merge (not mergeable / conflict / unmet checks) is a
    # client-state 409, not a 502 — and never an unhandled 500.
    _patch_merge(monkeypatch, merged=False)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_m4", "demo", status="ci_passed", ci_status="passed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_m4/merge", json={})
    assert resp.status_code == 409
