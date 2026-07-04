"""Sync a changeset's CI status from the repo's own checks (webhook or poll).

Moves ``pr_open → ci_running → ci_passed | ci_failed`` as the customer repo's CI
reports in, and promotes the draft PR to ready-for-review once green (decision
D5). Dependencies are injected so the path is testable without GitHub.

Two guards keep the CI gate *bounded* instead of trusting GitHub's signals
blindly (see ``github.checks`` for the evidence model):

- a grace window before acting on ``none`` (CI may exist but not have reported
  its first status yet);
- a deadline on *inferred* ``pending`` (evidence said CI should report, but
  nothing was ever observed on the ref — e.g. phantom app check-suites, or a
  workflow that never triggers on PR branches). Past the deadline the gate is
  released as ``ci_status="no_report"`` rather than held forever.

Both are anchored on ``ci_awaiting_since`` (set once when the PR opens), not on
``updated_at`` — status transitions refresh ``updated_at``, which would let the
sync reset its own clock.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import asyncpg

from app.config import codegen_ci_none_grace_seconds, codegen_ci_pending_timeout
from app.models.changeset import CI_SYNCABLE_STATUSES, ChangesetStatus
from app.store import changesets as store
from app.store import connections as connections_store

logger = logging.getLogger(__name__)

CIStatusReader = Callable[[str, str, str], Awaitable[str]]  # (repo, ref, token) -> status
TokenMinter = Callable[[int, str], Awaitable[str]]
ReadyMarker = Callable[..., Awaitable[None]]


def _seconds_awaiting(awaiting_since: datetime | None) -> float | None:
    """Seconds since the changeset started awaiting CI (``None`` if unknown).

    Naive timestamps are read as UTC.
    """
    if awaiting_since is None:
        return None
    if awaiting_since.tzinfo is None:
        awaiting_since = awaiting_since.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - awaiting_since).total_seconds()


def _within_none_grace(awaiting_since: datetime | None) -> bool:
    """True if a ``none`` result should still be held as pending."""
    grace = codegen_ci_none_grace_seconds()
    elapsed = _seconds_awaiting(awaiting_since)
    if grace <= 0 or elapsed is None:
        return False
    return elapsed < grace


def _pending_wait_expired(awaiting_since: datetime | None) -> bool:
    """True when an inferred ``pending`` has exhausted its deadline."""
    timeout = codegen_ci_pending_timeout()
    elapsed = _seconds_awaiting(awaiting_since)
    if timeout <= 0 or elapsed is None:
        return False
    return elapsed >= timeout


async def sync_ci_status(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    get_status: CIStatusReader,
    mint_token: TokenMinter,
    mark_ready: ReadyMarker | None = None,
) -> str | None:
    """Pull the latest CI status for a changeset's branch and advance its state.

    Returns the resolved CI status (``passed`` / ``failed`` / ``pending`` /
    ``none`` / ``no_report``), or ``None`` if the changeset is not in a state
    where CI applies. ``none`` means the repo has no CI configured and
    ``no_report`` means CI evidence existed but nothing ever reported within the
    deadline: in both cases there is nothing (left) to wait on, so the changeset
    advances to ``ci_passed`` (with the distinction preserved in ``ci_status``)
    and the Merge button is unblocked — a human still makes the merge decision.
    """
    changeset = await store.get_changeset(pool, changeset_id)
    if changeset is None or changeset.branch is None:
        return None
    if changeset.status not in CI_SYNCABLE_STATUSES:
        return None

    connection = await connections_store.get_connection(pool, changeset.project_id)
    if connection is None:
        return None

    token = await mint_token(connection.installation_id, connection.repo)
    status = await get_status(connection.repo, changeset.branch, token)
    # Evidence level (see github.checks.CIStatus). Plain strings — older
    # readers, test fakes — default to observed: the conservative reading,
    # since only inferred verdicts may be timed out.
    observed = bool(getattr(status, "observed", True))
    awaiting_since = changeset.ci_awaiting_since or changeset.updated_at

    # Still-failed fast path: CI already reported failed and still says failed.
    # Re-recording it would bounce ci_failed → ci_running → ci_failed, refreshing
    # updated_at on every poll — churn that defeats the poller's age cap
    # (CODEGEN_CI_SYNC_MAX_AGE_SECONDS) so a long-dead PR is re-polled forever.
    if status == "failed" and changeset.status is ChangesetStatus.ci_failed:
        return "failed"

    # A "none" result inside the grace window is most likely "CI hasn't reported
    # yet" rather than "repo has no CI" — commit-status-only CI registers no
    # check-suite/workflow until its first post (see config docstring). Hold it as
    # pending and leave the changeset in ci_running so a late status can still
    # demote it; only let "none" clear the gate once we've waited long enough.
    if status == "none" and _within_none_grace(awaiting_since):
        if changeset.status in (ChangesetStatus.pr_open, ChangesetStatus.ci_failed):
            await store.set_ci_status(
                pool, changeset_id, target=ChangesetStatus.ci_running, ci_status="pending"
            )
        logger.info(
            "CI reports 'none' for changeset %s but it is within the no-CI grace "
            "window; holding as pending pending a possible late status.",
            changeset_id,
        )
        return "pending"

    # Inferred-pending deadline: evidence said CI should report (live suites /
    # active workflows) but nothing was ever OBSERVED on the ref. Past the
    # deadline that inference is judged wrong — phantom suite, workflow that
    # never triggers on PR branches — and the gate is released as "no_report".
    # An observed pending (real CI executing) never times out, and a changeset
    # that once observed a failure (ci_failed) is never released this way.
    resolved = str(status)
    if (
        status == "pending"
        and not observed
        and changeset.status is not ChangesetStatus.ci_failed
        and _pending_wait_expired(awaiting_since)
    ):
        resolved = "no_report"
        logger.warning(
            "Changeset %s has awaited CI beyond the pending deadline with nothing "
            "observed on the ref; releasing the CI gate as 'no_report'.",
            changeset_id,
        )

    # Ensure we are in ci_running before recording a terminal CI result (also
    # handles a re-run after a previous ci_failed).
    if changeset.status in (ChangesetStatus.pr_open, ChangesetStatus.ci_failed):
        await store.set_ci_status(
            pool, changeset_id, target=ChangesetStatus.ci_running, ci_status="pending"
        )

    # "passed" (CI green), "none" (repo has no CI to wait on), and "no_report"
    # (CI never reported within the deadline) all clear the CI gate; the
    # ci_status column preserves which one it was for the audit/UI.
    if resolved in ("passed", "none", "no_report"):
        await store.set_ci_status(
            pool, changeset_id, target=ChangesetStatus.ci_passed, ci_status=resolved
        )
        if mark_ready is not None and changeset.pr_node_id:
            try:
                await mark_ready(node_id=changeset.pr_node_id, token=token)
            except Exception:
                logger.warning("Could not mark PR ready for changeset %s", changeset_id)
    elif resolved == "failed":
        await store.set_ci_status(
            pool, changeset_id, target=ChangesetStatus.ci_failed, ci_status="failed"
        )

    return resolved
