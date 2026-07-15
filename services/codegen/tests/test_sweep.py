"""Tests for the orphaned-changeset sweep + queued re-enqueue listing.

The fake has no clock, so these exercise the *status* filter — which lifecycle
states get swept to ``error`` and which are left alone. The ``older_than_seconds``
deadline itself is enforced by real SQL (``updated_at < now() - …``).
"""

import pytest

from app.models.changeset import ChangesetStatus
from app.store import changesets as store
from tests.fakes import FakePool

#: Active (post-claim) states a dead job leaves behind — these get swept.
_ACTIVE = ("cloning", "editing", "testing", "pushing")
#: Everything the sweep must never touch: queued (re-enqueued instead, since it
#: has produced nothing) and settled/terminal states.
_UNSWEPT = ("queued", "pr_open", "ci_running", "ci_passed", "merged", "tests_failed", "abandoned")

_ERROR = "orphaned by a restart"


@pytest.mark.asyncio
async def test_sweep_fails_only_active_states():
    pool = FakePool()
    for i, status in enumerate(_ACTIVE + _UNSWEPT):
        pool.add_changeset(f"cs_{i:02d}", "demo", status=status)

    swept = await store.fail_stale_changesets(
        pool, older_than_seconds=3600, error=_ERROR
    )

    # Every active row was swept; queued and settled rows were left alone.
    assert len(swept) == len(_ACTIVE)
    for i, status in enumerate(_ACTIVE + _UNSWEPT):
        final = await store.get_changeset(pool, f"cs_{i:02d}")
        if status in _ACTIVE:
            assert final.status == ChangesetStatus.error
            assert final.error == _ERROR
        else:
            assert final.status == ChangesetStatus(status)


@pytest.mark.asyncio
async def test_sweep_leaves_queued_for_the_requeue_path():
    # A queued orphan is not failed — startup re-enqueues it (it never started,
    # so re-running is safe; the queued → cloning claim dedupes).
    pool = FakePool()
    pool.add_changeset("cs_q", "demo", status="queued")

    swept = await store.fail_stale_changesets(
        pool, older_than_seconds=3600, error=_ERROR
    )

    assert swept == []
    assert (await store.get_changeset(pool, "cs_q")).status == ChangesetStatus.queued
    assert await store.list_queued_changeset_ids(pool) == ["cs_q"]


@pytest.mark.asyncio
async def test_sweep_is_a_noop_when_nothing_is_stuck():
    pool = FakePool()
    pool.add_changeset("cs_done", "demo", status="merged")

    swept = await store.fail_stale_changesets(
        pool, older_than_seconds=3600, error=_ERROR
    )

    assert swept == []
    assert (await store.get_changeset(pool, "cs_done")).status == ChangesetStatus.merged


@pytest.mark.asyncio
async def test_list_queued_ids_only_returns_queued():
    pool = FakePool()
    pool.add_changeset("cs_q1", "demo", status="queued")
    pool.add_changeset("cs_run", "demo", status="editing")
    pool.add_changeset("cs_done", "demo", status="merged")

    assert await store.list_queued_changeset_ids(pool) == ["cs_q1"]
