"""Endpoint tests for changeset merge (green-CI enforced, GitHub mocked)."""

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.github.app_auth import InstallationToken
from app.github.checks import CIStatus
from app.github.pulls import MergeResult
from app.main import app
from app.routers import changesets
from tests.fakes import FakePool


def _client(pool: FakePool, *, live_ci: str | None = None) -> AsyncClient:
    app.state.pg_pool = pool
    # ci_deps is set explicitly: other test modules leave it on the shared app
    # object, and the merge endpoint uses it for the live CI re-check.
    if live_ci is None:
        app.state.ci_deps = None
    else:

        async def get_status(repo: str, ref: str, token: str) -> str:
            return live_ci

        app.state.ci_deps = {"get_status": get_status, "mint_token": None, "mark_ready": None}
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
    body = resp.json()
    assert body["status"] == "merged"
    # The merge commit SHA is recorded — it is the deterministic /revert target.
    assert body["merge_sha"] == "abc123"
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
async def test_merge_succeeds_when_ci_never_reported(monkeypatch):
    # ci_status="no_report" means CI evidence existed but nothing ever reported
    # within the pending deadline — there is no verdict left to wait on, so the
    # (human/policy-gated) merge is allowed, like "none".
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=7)
    pool.add_changeset(
        "cs_nr", "demo", status="ci_passed", ci_status="no_report", pr_number=5, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_nr/merge", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"
    assert calls["merge"]["number"] == 5


@pytest.mark.asyncio
async def test_merge_refused_when_ci_pending(monkeypatch):
    # "pending" must still block — only "passed"/"none"/"no_report" clear the gate.
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
async def test_merge_refused_when_live_ci_disagrees(monkeypatch):
    # The stored ci_status says green, but the branch moved since it was
    # recorded and live CI reports failed — the stale stored status must not
    # authorize the merge.
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_live", "demo", status="ci_passed", ci_status="passed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool, live_ci="failed") as client:
        resp = await client.post("/v1/changesets/cs_live/merge", json={})
    assert resp.status_code == 409
    assert "stale" in resp.json()["detail"]
    assert "merge" not in calls  # GitHub merge was never attempted


@pytest.mark.asyncio
async def test_merge_proceeds_when_live_ci_confirms(monkeypatch):
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_live2", "demo", status="ci_passed", ci_status="passed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool, live_ci="passed") as client:
        resp = await client.post("/v1/changesets/cs_live2/merge", json={})
    assert resp.status_code == 200
    assert calls["merge"]["number"] == 5


@pytest.mark.asyncio
async def test_merge_refused_when_live_ci_pending_is_observed(monkeypatch):
    # A live OBSERVED pending — real runs executing on the ref right now (e.g.
    # a push since the stored result) — blocks the merge. Plain strings from the
    # reader default to observed, the conservative reading.
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_lp", "demo", status="ci_passed", ci_status="passed", pr_number=5, branch="apdl/x"
    )
    async with _client(pool, live_ci="pending") as client:
        resp = await client.post("/v1/changesets/cs_lp/merge", json={})
    assert resp.status_code == 409
    assert "merge" not in calls


@pytest.mark.asyncio
async def test_merge_proceeds_when_live_ci_pending_is_only_inferred(monkeypatch):
    # A live INFERRED pending (phantom app suites / dormant workflows — nothing
    # actually reported on the ref) must not re-block a gate the sync already
    # resolved: that CI is never going to report, and the merge would wedge.
    calls = _patch_merge(monkeypatch)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_li", "demo", status="ci_passed", ci_status="no_report", pr_number=5, branch="apdl/x"
    )
    async with _client(pool, live_ci=CIStatus("pending", observed=False)) as client:
        resp = await client.post("/v1/changesets/cs_li/merge", json={})
    assert resp.status_code == 200
    assert calls["merge"]["number"] == 5


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
