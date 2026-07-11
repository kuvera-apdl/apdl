"""Tests for the periodic CI poller (fake pool + injected status reader)."""

import asyncio

import pytest

from app.jobs.ci_poller import poll_ci_once, run_ci_poller
from app.models.changeset import ChangesetStatus
from app.store import changesets as store
from tests.fakes import FakePool


async def _mint(installation_id: int, repo: str) -> str:
    return "ghs_tok"


@pytest.mark.asyncio
async def test_poll_sweeps_only_syncable_and_advances_them():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    # Two syncable, one terminal (merged) that must be ignored.
    pool.add_changeset("cs_open", "demo", status="pr_open", pr_number=1, branch="apdl/a")
    pool.add_changeset("cs_run", "demo", status="ci_running", pr_number=2, branch="apdl/b")
    pool.add_changeset("cs_done", "demo", status="merged", pr_number=3, branch="apdl/c")

    async def get_status(repo, ref, token):
        return "none"  # repo has no CI → explicitly unverified

    swept = await poll_ci_once(pool, get_status=get_status, mint_token=_mint)

    assert swept == 2  # the merged one is not swept
    assert (await store.get_changeset(pool, "cs_open")).status == ChangesetStatus.unverified_external_ci
    assert (await store.get_changeset(pool, "cs_run")).status == ChangesetStatus.unverified_external_ci
    assert (await store.get_changeset(pool, "cs_open")).ci_status == "unverified_external_ci"
    # The terminal changeset is untouched.
    assert (await store.get_changeset(pool, "cs_done")).status == ChangesetStatus.merged


@pytest.mark.asyncio
async def test_poll_isolates_per_changeset_failures():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset("cs_bad", "demo", status="pr_open", pr_number=1, branch="apdl/a")
    pool.add_changeset("cs_good", "demo", status="pr_open", pr_number=2, branch="apdl/b")

    async def get_status(repo, ref, token):
        if ref == "apdl/a":
            raise RuntimeError("GitHub blew up")
        return "passed"

    swept = await poll_ci_once(pool, get_status=get_status, mint_token=_mint)

    assert swept == 2  # both attempted despite one raising
    # The healthy one still advanced.
    assert (await store.get_changeset(pool, "cs_good")).status == ChangesetStatus.ci_passed
    # The failing one stayed put (its error was swallowed, not propagated).
    assert (await store.get_changeset(pool, "cs_bad")).status == ChangesetStatus.pr_open


@pytest.mark.asyncio
async def test_run_ci_poller_loops_until_cancelled():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset("cs_open", "demo", status="pr_open", pr_number=1, branch="apdl/a")

    calls = 0

    async def get_status(repo, ref, token):
        nonlocal calls
        calls += 1
        return "passed"

    task = asyncio.create_task(
        run_ci_poller(pool, interval_seconds=0, get_status=get_status, mint_token=_mint)
    )
    # Let the loop run a few iterations, then cancel cleanly.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls >= 1  # the poller actually ran at least one sweep
