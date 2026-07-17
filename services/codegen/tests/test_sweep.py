"""Tests for the orphaned-changeset sweep + queued re-enqueue listing.

The fake has no clock, so these exercise the *status* filter — which lifecycle
states get swept to ``error`` and which are left alone. The ``older_than_seconds``
deadline itself is enforced by real SQL (``updated_at < now() - …``).
"""

import base64
from datetime import UTC, datetime

import pytest

from app.models.changeset import ChangesetStatus
from app.models.observations import ExternalCIStatus
from app.models.pr_publication import PublicationIntentRecorded
from app.store import changesets as store
from app.store import pr_publication as publication_store
from tests.fakes import FakePool

#: Active (post-claim) states a dead job leaves behind — these get swept.
_ACTIVE = ("cloning", "editing", "pushing")
#: Everything the sweep must never touch: queued (re-enqueued instead, since it
#: has produced nothing) and settled/terminal states.
_UNSWEPT = ("queued", "pr_open", "merged", "abandoned", "error")

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
async def test_sweep_preserves_pushing_row_with_durable_publication_intent():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_resume", "demo", status="pushing")
    await publication_store.record_intent(
        pool,
        PublicationIntentRecorded(
            event_id="cpub_" + "1" * 32,
            changeset_id="cs_resume",
            recorded_at=datetime(2026, 7, 16, tzinfo=UTC),
            repository="acme/widgets",
            repository_id=10,
            installation_id=1,
            branch="apdl/resume-cs_resume",
            base_branch="main",
            candidate_base_sha="a" * 40,
            candidate_head_sha="c" * 40,
            candidate_tree_sha="b" * 40,
            patch_base64=base64.b64encode(b"patch").decode(),
            commit_title="Resume",
            pull_request_title="Resume",
            pull_request_body="body",
            draft=True,
            external_ci_status=ExternalCIStatus.pending,
            diff_stat={"files": 1},
        ),
    )

    swept = await store.fail_stale_changesets(
        pool, older_than_seconds=3600, error=_ERROR
    )

    assert swept == []
    assert (
        await store.get_changeset(pool, "cs_resume")
    ).status is ChangesetStatus.pushing
    assert await publication_store.list_recoverable_ids(
        pool, older_than_seconds=3600
    ) == ["cs_resume"]


@pytest.mark.asyncio
async def test_list_queued_ids_only_returns_queued():
    pool = FakePool()
    pool.add_changeset("cs_q1", "demo", status="queued")
    pool.add_changeset("cs_run", "demo", status="editing")
    pool.add_changeset("cs_done", "demo", status="merged")

    assert await store.list_queued_changeset_ids(pool) == ["cs_q1"]
