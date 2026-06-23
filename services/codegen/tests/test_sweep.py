"""Tests for the orphaned-changeset startup sweep (fake pool, no clock).

The fake has no clock, so these exercise the *status* filter — which lifecycle
states get swept to ``error`` and which are left alone. The ``older_than_seconds``
deadline itself is enforced by real SQL (``updated_at < now() - …``).
"""

import pytest

from app.models.changeset import ChangesetStatus
from app.store import changesets as store
from tests.fakes import FakePool

_TRANSIENT = ("queued", "cloning", "editing", "testing", "pushing")
_SETTLED = ("pr_open", "ci_running", "ci_passed", "merged", "tests_failed", "abandoned")

_ERROR = "orphaned by a restart"


@pytest.mark.asyncio
async def test_sweep_fails_only_transient_states():
    pool = FakePool()
    for i, status in enumerate(_TRANSIENT + _SETTLED):
        pool.add_changeset(f"cs_{i:02d}", "demo", status=status)

    swept = await store.fail_stale_changesets(
        pool, older_than_seconds=3600, error=_ERROR
    )

    # Every transient row was swept; nothing else was touched.
    assert len(swept) == len(_TRANSIENT)
    for i, status in enumerate(_TRANSIENT + _SETTLED):
        final = await store.get_changeset(pool, f"cs_{i:02d}")
        if status in _TRANSIENT:
            assert final.status == ChangesetStatus.error
            assert final.error == _ERROR
        else:
            assert final.status == ChangesetStatus(status)


@pytest.mark.asyncio
async def test_sweep_is_a_noop_when_nothing_is_stuck():
    pool = FakePool()
    pool.add_changeset("cs_done", "demo", status="merged")

    swept = await store.fail_stale_changesets(
        pool, older_than_seconds=3600, error=_ERROR
    )

    assert swept == []
    assert (await store.get_changeset(pool, "cs_done")).status == ChangesetStatus.merged
