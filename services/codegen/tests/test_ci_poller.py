"""Recovery polling for GitHub-owned pull-request and CI state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.jobs import ci_poller
from tests.fakes import FakePool


async def _mint(_installation_id: int, _repo: str) -> str:
    return "ghs_tok"


async def _get_pr(_repo: str, _number: int, _token: str) -> dict:
    raise AssertionError("sync is replaced by the focused poller seam")


async def _get_ci(_repo: str, _head_sha: str, _token: str):
    raise AssertionError("sync is replaced by the focused poller seam")


def _seed_open_pr(
    pool: FakePool,
    changeset_id: str,
    *,
    external_ci_status: str,
    github_pr_status: str = "open",
    age_days: int = 0,
) -> None:
    pool.add_changeset(
        changeset_id,
        "demo",
        status="pr_open",
        pr_number=len(pool.store["changesets"]) + 1,
        branch=f"apdl/{changeset_id}",
        head_sha=f"head-{changeset_id}",
        github_pr_status=github_pr_status,
        external_ci_status=external_ci_status,
        external_ci_awaiting_since=datetime.now(timezone.utc)
        - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_poll_recovers_every_open_pr_independent_of_ci_projection_or_age(
    monkeypatch,
):
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    _seed_open_pr(pool, "cs-pending", external_ci_status="pending")
    _seed_open_pr(pool, "cs-passed", external_ci_status="passed")
    _seed_open_pr(pool, "cs-failed", external_ci_status="failed")
    _seed_open_pr(
        pool,
        "cs-unverified-old",
        external_ci_status="unverified_external_ci",
        github_pr_status="draft",
        age_days=365,
    )
    pool.add_changeset(
        "cs-merged",
        "demo",
        status="merged",
        pr_number=99,
        head_sha="head-merged",
        github_pr_status="merged",
        external_ci_status="passed",
        merge_sha="merge-sha",
    )
    pool.add_changeset(
        "cs-closed",
        "demo",
        status="abandoned",
        pr_number=100,
        head_sha="head-closed",
        github_pr_status="closed",
        external_ci_status="failed",
    )
    synced: list[str] = []

    async def fake_sync(_pool, changeset_id, **_kwargs):
        synced.append(changeset_id)

    monkeypatch.setattr(ci_poller, "sync_github_state", fake_sync)

    swept = await ci_poller.poll_github_once(
        pool,
        get_pull_request=_get_pr,
        get_ci_evidence=_get_ci,
        mint_token=_mint,
    )

    assert swept == 4
    assert set(synced) == {
        "cs-pending",
        "cs-passed",
        "cs-failed",
        "cs-unverified-old",
    }
    assert "cs-merged" not in synced
    assert "cs-closed" not in synced


@pytest.mark.asyncio
async def test_poll_isolates_one_github_recovery_failure(monkeypatch):
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    _seed_open_pr(pool, "cs-bad", external_ci_status="pending")
    _seed_open_pr(pool, "cs-good", external_ci_status="unverified_external_ci")
    attempted: list[str] = []

    async def fake_sync(_pool, changeset_id, **_kwargs):
        attempted.append(changeset_id)
        if changeset_id == "cs-bad":
            raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(ci_poller, "sync_github_state", fake_sync)

    swept = await ci_poller.poll_github_once(
        pool,
        get_pull_request=_get_pr,
        get_ci_evidence=_get_ci,
        mint_token=_mint,
    )

    assert swept == 2
    assert set(attempted) == {"cs-bad", "cs-good"}


@pytest.mark.asyncio
async def test_run_github_poller_loops_until_cancelled(monkeypatch):
    calls = 0

    async def fake_poll(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(ci_poller, "poll_github_once", fake_poll)
    task = asyncio.create_task(
        ci_poller.run_github_poller(
            FakePool(),
            interval_seconds=0,
            get_pull_request=_get_pr,
            get_ci_evidence=_get_ci,
            mint_token=_mint,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 1
