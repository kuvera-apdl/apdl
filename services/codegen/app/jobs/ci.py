"""Sync a changeset's CI status from the repo's own checks (webhook or poll).

Moves ``pr_open → ci_running → ci_passed | ci_failed`` as the customer repo's CI
reports in, and promotes the draft PR to ready-for-review once green (decision
D5). Dependencies are injected so the path is testable without GitHub.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import asyncpg

from app.config import codegen_ci_none_grace_seconds
from app.models.changeset import CI_SYNCABLE_STATUSES, ChangesetStatus
from app.store import changesets as store
from app.store import connections as connections_store

logger = logging.getLogger(__name__)

CIStatusReader = Callable[[str, str, str], Awaitable[str]]  # (repo, ref, token) -> status
TokenMinter = Callable[[int, str], Awaitable[str]]
ReadyMarker = Callable[..., Awaitable[None]]


def _within_none_grace(awaiting_since: datetime | None) -> bool:
    """True if a ``none`` result should still be held as pending.

    ``awaiting_since`` is the changeset's ``updated_at`` — when it entered (or last
    re-entered) the CI-waiting states. Naive timestamps are read as UTC.
    """
    grace = codegen_ci_none_grace_seconds()
    if grace <= 0 or awaiting_since is None:
        return False
    if awaiting_since.tzinfo is None:
        awaiting_since = awaiting_since.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - awaiting_since).total_seconds()
    return elapsed < grace


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
    ``none``), or ``None`` if the changeset is not in a state where CI applies.
    ``none`` means the repo has no CI configured: there is nothing to wait on, so
    the changeset advances to ``ci_passed`` (recorded as ``ci_status="none"``)
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

    # A "none" result inside the grace window is most likely "CI hasn't reported
    # yet" rather than "repo has no CI" — commit-status-only CI registers no
    # check-suite/workflow until its first post (see config docstring). Hold it as
    # pending and leave the changeset in ci_running so a late status can still
    # demote it; only let "none" clear the gate once we've waited long enough.
    if status == "none" and _within_none_grace(changeset.updated_at):
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

    # Ensure we are in ci_running before recording a terminal CI result (also
    # handles a re-run after a previous ci_failed).
    if changeset.status in (ChangesetStatus.pr_open, ChangesetStatus.ci_failed):
        await store.set_ci_status(
            pool, changeset_id, target=ChangesetStatus.ci_running, ci_status="pending"
        )

    # "passed" (CI green) and "none" (repo has no CI to wait on) both clear the
    # CI gate; the ci_status column preserves which one it was for the audit/UI.
    if status in ("passed", "none"):
        await store.set_ci_status(
            pool, changeset_id, target=ChangesetStatus.ci_passed, ci_status=status
        )
        if mark_ready is not None and changeset.pr_node_id:
            try:
                await mark_ready(node_id=changeset.pr_node_id, token=token)
            except Exception:
                logger.warning("Could not mark PR ready for changeset %s", changeset_id)
    elif status == "failed":
        await store.set_ci_status(
            pool, changeset_id, target=ChangesetStatus.ci_failed, ci_status="failed"
        )

    return status
