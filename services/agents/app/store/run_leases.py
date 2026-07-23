"""Replica-safe ownership and requeueing for durable agent runs.

Every replica dispatches ownerless PostgreSQL queue rows. A lease makes a
process crash observable without treating work owned by another healthy
replica as dead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Literal, TypeVar

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


class RunLeaseLostError(RuntimeError):
    """Raised when a supervisor no longer owns its run."""


class RunCancellationNotFoundError(LookupError):
    """Raised when the requested tenant-scoped run does not exist."""


class RunCancellationConflictError(RuntimeError):
    """Raised when a completed run cannot truthfully become cancelled."""


@dataclass(frozen=True)
class RecoveryResult:
    requeued_run_ids: tuple[str, ...]
    reopened_proposal_ids: tuple[str, ...]


@dataclass(frozen=True)
class RunCancellationResult:
    run_id: str
    previous_status: str
    status: Literal["cancelled", "cancelling"] = "cancelled"


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
              AND execution_lane_project_id = project_id
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
              AND execution_lane_project_id = project_id
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
              AND execution_lane_project_id = project_id
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


async def cancel_run(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    project_id: str,
    actor_credential_id: str,
    actor_user_id: str | None = None,
) -> RunCancellationResult:
    """Atomically cancel live work, its queued effects, and its proposal claims.

    Never-started effects are cancelled in place. Effects that crossed an
    egress boundary keep the run in ``cancelling`` so its generated project
    lane remains occupied until their stable identities are reconciled.
    Claim, cancellation, and settlement all lock the run row first.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT status, phase, execution_lane_project_id
                FROM agent_runs
                WHERE run_id = $1 AND project_id = $2
                FOR UPDATE
                """,
                run_id,
                project_id,
            )
            if row is None:
                raise RunCancellationNotFoundError(f"Run {run_id} not found")

            previous_status = str(row["status"])
            if previous_status == "cancelled":
                return RunCancellationResult(run_id, previous_status)
            phase = str(row["phase"] or "")
            if row["execution_lane_project_id"] != project_id:
                raise RunCancellationConflictError(
                    f"Run {run_id} is already terminal with status {previous_status}"
                )

            live_effects = await conn.fetch(
                """
                SELECT effect.effect_id, effect.status
                FROM agent_approval_effects AS effect
                JOIN agent_approval_commands AS command
                  ON command.command_id = effect.command_id
                WHERE command.run_id = $1 AND command.project_id = $2
                  AND effect.status IN (
                      'queued', 'processing', 'retryable_failed'
                  )
                FOR UPDATE OF effect
                """,
                run_id,
                project_id,
            )
            pending_statuses = {
                str(effect["status"])
                for effect in live_effects
                if str(effect["status"]) in {"processing", "retryable_failed"}
            }
            cancellation_pending = bool(pending_statuses)
            if "processing" in pending_statuses:
                target_phase = "cancellation_draining"
            elif cancellation_pending:
                target_phase = "cancellation_reconciliation"
            else:
                target_phase = "cancelled"
            target_status: Literal["cancelled", "cancelling"] = (
                "cancelling" if cancellation_pending else "cancelled"
            )

            await conn.execute(
                """
                UPDATE agent_approval_effects AS effect
                SET status = 'manual_intervention',
                    last_error = 'Owning run was cancelled before effect claim',
                    lease_owner_id = NULL, lease_expires_at = NULL,
                    completed_at = now(), updated_at = now()
                FROM agent_approval_commands AS command
                WHERE effect.command_id = command.command_id
                  AND command.run_id = $1 AND command.project_id = $2
                  AND effect.status = 'queued'
                """,
                run_id,
                project_id,
            )
            await conn.execute(
                """
                UPDATE agent_approval_commands
                SET status = 'manual_intervention',
                    last_error = 'Owning run was cancelled',
                    completed_at = now(), updated_at = now()
                WHERE run_id = $1 AND project_id = $2
                  AND status IN ('queued', 'processing')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM agent_approval_effects AS effect
                      WHERE effect.command_id = agent_approval_commands.command_id
                        AND effect.status IN ('processing', 'retryable_failed')
                  )
                """,
                run_id,
                project_id,
            )
            if not cancellation_pending:
                await conn.execute(
                    """
                    UPDATE feature_proposals
                    SET status = 'approved', claim_run_id = NULL,
                        error = NULL, updated_at = now()
                    WHERE project_id = $2 AND claim_run_id = $1
                      AND status = 'implementing'
                    """,
                    run_id,
                    project_id,
                )
            await conn.execute(
                """
                UPDATE agent_runs
                SET status = $3, phase = $4,
                    lease_owner_id = NULL, lease_expires_at = NULL,
                    updated_at = now()
                WHERE run_id = $1 AND project_id = $2
                  AND execution_lane_project_id = $2
                """,
                run_id,
                project_id,
                target_status,
                target_phase,
            )
            audit_action = (
                "run_cancellation_requested"
                if cancellation_pending
                else "run_cancelled"
            )
            audit_key = (
                f"run-cancellation-requested:{run_id}"
                if cancellation_pending
                else f"run-cancelled:{run_id}"
            )
            await conn.execute(
                """
                INSERT INTO agent_audit_log (
                    run_id, action_type, config, safety_result, approval_status,
                    idempotency_key
                )
                VALUES (
                    $1, $5, $2::jsonb, $3::jsonb, $6, $4
                )
                ON CONFLICT (run_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL
                DO NOTHING
                """,
                run_id,
                json.dumps(
                    {
                        "actor_credential_id": actor_credential_id,
                        "actor_user_id": actor_user_id,
                        "previous_status": previous_status,
                        "previous_phase": phase,
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "passed": True,
                        "checks": ["operator_requested_cancellation"],
                    },
                    sort_keys=True,
                ),
                audit_key,
                audit_action,
                target_status,
            )

    return RunCancellationResult(run_id, previous_status, target_status)


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


async def requeue_expired_runs(
    pool: asyncpg.Pool,
    *,
    legacy_stale_seconds: int = LEGACY_RUN_STALE_SECONDS,
    recovery_grace_seconds: int = RUN_RECOVERY_GRACE_SECONDS,
    legacy_proposal_stale_seconds: int = LEGACY_PROPOSAL_STALE_SECONDS,
) -> RecoveryResult:
    """Clear expired ownership while preserving resumable run state.

    Status, phase, and persisted results remain unchanged for active runs. An
    ownerless row is immediately visible to every replica's dispatcher, whose
    supervisor resumes completed phases from ``agent_run_results``. Claims
    owned by terminal runs are reopened; live and approval-gated claims remain
    untouched. Legacy owners without an expiry use the conservative 24-hour
    cutoff.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE agent_runs
                SET lease_owner_id = NULL,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE (status IN ('started', 'running')
                       OR (phase = 'resuming' AND status IN ('approved', 'rejected')))
                  AND execution_lane_project_id = project_id
                  AND lease_owner_id IS NOT NULL
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
            requeued = tuple(str(row["run_id"]) for row in rows)

            reopened_ids: list[str] = []

            # A run can terminate after claiming a proposal but before the
            # proposal reaches its own terminal state.  Such a claim has no
            # live supervisor left to resume it, so return only those exact
            # run-owned rows to the approved queue.  Do not infer abandonment
            # from age: waiting-approval and live runs may legitimately retain
            # a claim indefinitely.
            terminal_claim_rows = await conn.fetch(
                """
                UPDATE feature_proposals AS proposal
                SET status = 'approved',
                    claim_run_id = NULL,
                    error = NULL,
                    updated_at = now()
                FROM agent_runs AS claim_run
                WHERE proposal.status = 'implementing'
                  AND proposal.claim_run_id = claim_run.run_id
                  AND proposal.project_id = claim_run.project_id
                  AND claim_run.status IN (
                      'completed', 'completed_with_errors', 'failed', 'cancelled'
                  )
                RETURNING proposal.proposal_id
                """
            )
            reopened_ids.extend(str(row["proposal_id"]) for row in terminal_claim_rows)

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
                      WHERE active_run.execution_lane_project_id = proposal.project_id
                  )
                RETURNING proposal.proposal_id
                """,
                legacy_proposal_stale_seconds,
            )
            reopened_ids.extend(str(row["proposal_id"]) for row in legacy_rows)

            reopened = tuple(dict.fromkeys(reopened_ids))

    return RecoveryResult(requeued, reopened)


async def requeue_expired_runs_forever(
    pool: asyncpg.Pool,
    stop: asyncio.Event,
    *,
    interval_seconds: int = RUN_REAPER_SECONDS,
) -> None:
    """Periodically requeue expired leases; safe on every replica."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            result = await requeue_expired_runs(pool)
            if result.requeued_run_ids or result.reopened_proposal_ids:
                logger.warning(
                    "Lease recovery requeued %d run(s) and reopened %d proposal claim(s)",
                    len(result.requeued_run_ids),
                    len(result.reopened_proposal_ids),
                )
        except Exception:
            logger.exception("Agent run lease requeue failed")
