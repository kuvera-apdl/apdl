"""Replica-safe ownership and recovery for in-process agent runs.

Supervisors execute as FastAPI background tasks, so a process crash abandons
the task. A lease makes that abandonment observable without treating every run
owned by another healthy replica as dead when this replica starts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TypeVar

import asyncpg

logger = logging.getLogger(__name__)

RUN_LEASE_SECONDS = 5 * 60
RUN_HEARTBEAT_SECONDS = 30
RUN_REAPER_SECONDS = 30
RUN_LEASE_EXPIRY_SAFETY_SECONDS = 5
RUN_RECOVERY_GRACE_SECONDS = RUN_HEARTBEAT_SECONDS + RUN_LEASE_EXPIRY_SAFETY_SECONDS
LEGACY_RUN_STALE_SECONDS = 24 * 60 * 60
LEGACY_PROPOSAL_STALE_SECONDS = 24 * 60 * 60

_T = TypeVar("_T")

AGENT_RUN_LEASE_MIGRATE_DDL = """
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS lease_owner_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE agent_runs
    ALTER COLUMN lease_expires_at
    DROP DEFAULT;
CREATE INDEX IF NOT EXISTS idx_agent_runs_lease_expiry
    ON agent_runs (lease_expires_at)
    WHERE status IN ('started', 'running')
       OR (phase = 'resuming' AND status IN ('approved', 'rejected'));
"""


class RunLeaseLostError(RuntimeError):
    """Raised when a supervisor no longer owns its run."""


@dataclass(frozen=True)
class RecoveryResult:
    abandoned_run_ids: tuple[str, ...]
    reopened_proposal_ids: tuple[str, ...]


def new_lease_owner_id() -> str:
    """Return a task-unique owner id that is useful in operational logs."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


async def acquire_run_lease(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    *,
    lease_seconds: int = RUN_LEASE_SECONDS,
    recovery_grace_seconds: int = RUN_RECOVERY_GRACE_SECONDS,
) -> bool:
    """Atomically claim an active run when it is queued or safely abandoned.

    Ownerless rows are explicit queued work and can be claimed immediately.
    Expired, non-NULL owners retain a grace period so their local expiry timer
    can cancel in-flight work before a replacement supervisor starts.
    """
    async with pool.acquire() as conn:
        claimed = await conn.fetchval(
            """
            UPDATE agent_runs
            SET lease_owner_id = $2,
                lease_expires_at = now() + ($3 * interval '1 second'),
                updated_at = now()
            WHERE run_id = $1
              AND (status IN ('started', 'running')
                   OR (phase = 'resuming' AND status IN ('approved', 'rejected')))
              AND (lease_owner_id IS NULL
                   OR (lease_expires_at IS NOT NULL
                       AND lease_expires_at <= now() - ($4 * interval '1 second')))
            RETURNING run_id
            """,
            run_id,
            owner_id,
            lease_seconds,
            recovery_grace_seconds,
        )
    return claimed is not None


async def renew_run_lease(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    *,
    lease_seconds: int = RUN_LEASE_SECONDS,
) -> bool:
    """Extend a lease only while this task still owns an active run."""
    async with pool.acquire() as conn:
        renewed = await conn.fetchval(
            """
            UPDATE agent_runs
            SET lease_expires_at = now() + ($3 * interval '1 second'),
                updated_at = now()
            WHERE run_id = $1
              AND lease_owner_id = $2
              AND lease_expires_at > now()
              AND (status IN ('started', 'running')
                   OR (phase = 'resuming' AND status IN ('approved', 'rejected')))
            RETURNING run_id
            """,
            run_id,
            owner_id,
            lease_seconds,
        )
    return renewed is not None


async def handoff_run_to_queue(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    *,
    lease_seconds: int = RUN_LEASE_SECONDS,
) -> bool:
    """Atomically replace owned approval work with a recoverable queue lease.

    The owner is cleared only after all approval effects finish. The fresh
    expiry makes a crash between this handoff and task scheduling recoverable,
    while ``acquire_run_lease`` may immediately claim the ownerless queue row.
    """
    async with pool.acquire() as conn:
        queued = await conn.fetchval(
            """
            UPDATE agent_runs
            SET lease_owner_id = NULL,
                lease_expires_at = now() + ($3 * interval '1 second'),
                updated_at = now()
            WHERE run_id = $1
              AND lease_owner_id = $2
              AND lease_expires_at > now()
              AND phase = 'resuming'
              AND status IN ('approved', 'rejected')
            RETURNING run_id
            """,
            run_id,
            owner_id,
            lease_seconds,
        )
    return queued is not None


async def maintain_run_lease(
    pool: asyncpg.Pool,
    run_id: str,
    owner_id: str,
    stop: asyncio.Event,
    lost: asyncio.Event,
    *,
    lease_seconds: int = RUN_LEASE_SECONDS,
    heartbeat_seconds: float = RUN_HEARTBEAT_SECONDS,
    expiry_safety_seconds: float = RUN_LEASE_EXPIRY_SAFETY_SECONDS,
    confirmed_at: float | None = None,
) -> None:
    """Renew ownership and signal loss by the locally confirmed deadline.

    A database error is not itself proof of loss, but it also cannot extend the
    last confirmed lease. Renewal calls are therefore bounded by the remaining
    local lifetime; hangs and repeated errors both signal loss before the
    database lease can be reaped or stolen after its recovery grace.
    """
    loop = asyncio.get_running_loop()
    confirmed_for = max(0.0, float(lease_seconds) - expiry_safety_seconds)
    confirmed_until = (
        confirmed_at if confirmed_at is not None else loop.time()
    ) + confirmed_for

    def _mark_lost(reason: str) -> None:
        if not lost.is_set():
            lost.set()
            logger.error(
                "[%s] Agent run lease lost by %s: %s", run_id, owner_id, reason
            )

    while not stop.is_set():
        remaining = confirmed_until - loop.time()
        if remaining <= 0:
            _mark_lost("local confirmed lease expired")
            return

        try:
            await asyncio.wait_for(
                stop.wait(), timeout=min(float(heartbeat_seconds), remaining)
            )
            return
        except TimeoutError:
            pass

        remaining = confirmed_until - loop.time()
        if remaining <= 0:
            _mark_lost("local confirmed lease expired")
            return

        renewal_started_at = loop.time()
        renew_task = asyncio.create_task(
            renew_run_lease(
                pool,
                run_id,
                owner_id,
                lease_seconds=lease_seconds,
            )
        )
        stop_task = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {renew_task, stop_task},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                return
            if renew_task not in done:
                _mark_lost("renewal did not finish before local expiry")
                return
            try:
                renewed = renew_task.result()
            except Exception:
                logger.exception("[%s] Could not renew agent run lease", run_id)
                if loop.time() >= confirmed_until:
                    _mark_lost("renewal errors exhausted local lease lifetime")
                    return
                continue
            if not renewed:
                _mark_lost("database rejected renewal")
                return
            # The query may return slowly after PostgreSQL renewed the row.
            # Measuring from request start is conservative; measuring from the
            # response would incorrectly add network delay to the known lease.
            confirmed_until = renewal_started_at + confirmed_for
        finally:
            for task in (renew_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(renew_task, stop_task, return_exceptions=True)


async def run_while_lease_owned(
    work: Awaitable[_T],
    lost: asyncio.Event,
    *,
    run_id: str,
) -> _T:
    """Race cancellable work against lease loss and fence all later effects.

    Cancellation prevents follow-up Config/Codegen calls after loss, but cannot
    undo a request an external service already accepted. Callers must continue
    to send stable run/proposal/experiment identity; exactly-once external
    effects remain the separate AUD-039 idempotency boundary.
    """
    work_task = asyncio.ensure_future(work)
    lost_task = asyncio.create_task(lost.wait())
    try:
        await asyncio.wait(
            {work_task, lost_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lost.is_set():
            if not work_task.done():
                work_task.cancel()
            await asyncio.gather(work_task, return_exceptions=True)
            raise RunLeaseLostError(f"Run {run_id} lease expired during execution")
        return await work_task
    finally:
        if not lost_task.done():
            lost_task.cancel()
        if not work_task.done():
            work_task.cancel()
        await asyncio.gather(work_task, lost_task, return_exceptions=True)


async def recover_abandoned_runs(
    pool: asyncpg.Pool,
    *,
    legacy_stale_seconds: int = LEGACY_RUN_STALE_SECONDS,
    recovery_grace_seconds: int = RUN_RECOVERY_GRACE_SECONDS,
    legacy_proposal_stale_seconds: int = LEGACY_PROPOSAL_STALE_SECONDS,
) -> RecoveryResult:
    """Fail only expired active runs and reopen only their claimed proposals.

    Rows created by lease-aware builds explicitly carry ``lease_expires_at``
    even before their task claims them, so a process that dies between INSERT
    and task start is recovered after the normal lease window plus grace. The
    column deliberately has no database default: an older replica running
    during a rolling upgrade must continue to insert NULL, which selects the
    conservative 24-hour legacy path instead of pretending it can heartbeat.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_runs
                SET status = 'failed',
                    phase = 'orphaned',
                    lease_owner_id = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE (status IN ('started', 'running')
                       OR (phase = 'resuming' AND status IN ('approved', 'rejected')))
                  AND (
                      (lease_expires_at IS NOT NULL
                       AND lease_expires_at <= now() - ($2 * interval '1 second'))
                      OR (
                          lease_expires_at IS NULL
                          AND updated_at <= now() - ($1 * interval '1 second')
                      )
                  )
                RETURNING run_id
                """,
                legacy_stale_seconds,
                recovery_grace_seconds,
            )
            abandoned = tuple(str(row["run_id"]) for row in rows)

            reopened_ids: list[str] = []
            if abandoned:
                proposal_rows = await conn.fetch(
                    """
                    UPDATE feature_proposals
                    SET status = 'approved',
                        claim_run_id = NULL,
                        error = NULL,
                        updated_at = now()
                    WHERE status = 'implementing'
                      AND claim_run_id = ANY($1::text[])
                    RETURNING proposal_id
                    """,
                    list(abandoned),
                )
                reopened_ids.extend(str(row["proposal_id"]) for row in proposal_rows)

            # Rolling upgrades can leave pre-claim_run_id rows permanently in
            # implementing. Reopen only old NULL claims, and only when the
            # project has no active run that could still own them. This may
            # delay recovery behind unrelated project work, which is preferable
            # to implementing a proposal while an old approval gate is live.
            legacy_rows = await conn.fetch(
                """
                UPDATE feature_proposals AS proposal
                SET status = 'approved',
                    claim_run_id = NULL,
                    error = NULL,
                    updated_at = now()
                WHERE proposal.status = 'implementing'
                  AND proposal.claim_run_id IS NULL
                  AND proposal.updated_at <= now() - ($1 * interval '1 second')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_runs AS active_run
                      WHERE active_run.project_id = proposal.project_id
                        AND (
                            active_run.status IN ('started', 'running', 'waiting_approval')
                            OR (
                                active_run.phase = 'resuming'
                                AND active_run.status IN ('approved', 'rejected')
                            )
                        )
                  )
                RETURNING proposal.proposal_id
                """,
                legacy_proposal_stale_seconds,
            )
            reopened_ids.extend(str(row["proposal_id"]) for row in legacy_rows)

            reopened = tuple(dict.fromkeys(reopened_ids))

    return RecoveryResult(abandoned, reopened)


async def reap_abandoned_runs_forever(
    pool: asyncpg.Pool,
    stop: asyncio.Event,
    *,
    interval_seconds: int = RUN_REAPER_SECONDS,
) -> None:
    """Periodically reconcile expired leases; safe to run on every replica."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            result = await recover_abandoned_runs(pool)
            if result.abandoned_run_ids or result.reopened_proposal_ids:
                logger.warning(
                    "Lease recovery marked %d abandoned run(s) failed and reopened %d proposal(s)",
                    len(result.abandoned_run_ids),
                    len(result.reopened_proposal_ids),
                )
        except Exception:
            logger.exception("Agent run lease recovery failed")
