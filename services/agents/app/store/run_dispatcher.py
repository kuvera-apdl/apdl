"""PostgreSQL-backed dispatcher for durable agent runs.

``agent_runs`` is the queue.  Trigger and approval paths only insert or hand
off ownerless active rows; every service replica polls those rows and starts a
local supervisor task.  The supervisor's lease acquisition is the atomic claim
that makes duplicate polling across replicas harmless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.graphs.supervisor import run_supervisor
from app.memory.pgvector_store import PgVectorStore

logger = logging.getLogger(__name__)

RUN_DISPATCH_INTERVAL_SECONDS = 1.0
RUN_DISPATCH_BATCH_SIZE = 32


@dataclass(frozen=True)
class DispatchableRun:
    run_id: str
    project_id: str
    analysis_types: tuple[str, ...]
    time_range_days: int
    autonomy_level: int
    resume: bool
    resume_after_approval: bool
    target_proposal_id: str | None


def _parse_run(row: Any) -> DispatchableRun:
    raw_config = row["config"]
    if isinstance(raw_config, str):
        raw_config = json.loads(raw_config)
    if not isinstance(raw_config, dict):
        raise ValueError("agent run config must be an object")

    raw_types = raw_config.get("analysis_types")
    if (
        not isinstance(raw_types, list)
        or not raw_types
        or any(not isinstance(item, str) or not item.strip() for item in raw_types)
    ):
        raise ValueError("agent run config.analysis_types must be non-empty strings")

    time_range_days = raw_config.get("time_range_days")
    if (
        isinstance(time_range_days, bool)
        or not isinstance(time_range_days, int)
        or not 1 <= time_range_days <= 90
    ):
        raise ValueError("agent run config.time_range_days must be between 1 and 90")

    target_proposal_id = raw_config.get("target_proposal_id")
    if target_proposal_id is not None and (
        not isinstance(target_proposal_id, str) or not target_proposal_id.strip()
    ):
        raise ValueError("agent run config.target_proposal_id must be a non-empty string")

    status = str(row["status"])
    phase = str(row["phase"] or "initializing")
    resume_after_approval = phase == "resuming" and status in {"approved", "rejected"}
    return DispatchableRun(
        run_id=str(row["run_id"]),
        project_id=str(row["project_id"]),
        analysis_types=tuple(raw_types),
        time_range_days=time_range_days,
        autonomy_level=int(row["autonomy_level"]),
        resume=phase != "initializing" or status != "started",
        resume_after_approval=resume_after_approval,
        target_proposal_id=(
            target_proposal_id.strip() if isinstance(target_proposal_id, str) else None
        ),
    )


async def _quarantine_invalid_run(conn: Any, row: Any, error: Exception) -> bool:
    """Atomically terminalize one unchanged poison queue row and audit why.

    The config comparison prevents an operator repair racing this poll from
    being overwritten.  The data-modifying CTE keeps the terminal transition
    and its authoritative audit record in one PostgreSQL statement: either
    both persist, or neither does.
    """
    raw_config = row["config"]
    persisted_config = (
        raw_config
        if isinstance(raw_config, str)
        else json.dumps(raw_config, sort_keys=True, separators=(",", ":"))
    )
    error_message = f"Invalid persisted run config: {error}"
    audit_config = json.dumps(
        {
            "error": error_message,
            "terminal_phase": "invalid_config",
            "terminal_status": "failed",
        },
        sort_keys=True,
    )
    safety_result = json.dumps(
        {
            "checks": [
                {
                    "message": error_message,
                    "name": "persisted_run_config",
                    "passed": False,
                }
            ],
            "passed": False,
        },
        sort_keys=True,
    )
    config_sha256 = hashlib.sha256(persisted_config.encode("utf-8")).hexdigest()
    quarantined_run_id = await conn.fetchval(
        """
        WITH quarantined AS (
            UPDATE agent_runs
            SET status = 'failed',
                phase = 'invalid_config',
                lease_owner_id = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE run_id = $1
              AND project_id = $2
              AND lease_owner_id IS NULL
              AND (
                  status IN ('started', 'running')
                  OR (phase = 'resuming' AND status IN ('approved', 'rejected'))
              )
              AND config IS NOT DISTINCT FROM $3::jsonb
            RETURNING run_id
        ), audited AS (
            INSERT INTO agent_audit_log (
                run_id, action_type, config, safety_result, approval_status,
                idempotency_key
            )
            SELECT run_id, 'run_dispatch_invalid_config', $4::jsonb, $5::jsonb,
                   NULL, $6
            FROM quarantined
            ON CONFLICT (run_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL
            DO NOTHING
            RETURNING run_id
        )
        SELECT quarantined.run_id
        FROM quarantined
        LEFT JOIN audited USING (run_id)
        """,
        str(row["run_id"]),
        str(row["project_id"]),
        persisted_config,
        audit_config,
        safety_result,
        f"run-dispatch-invalid-config:{config_sha256}",
    )
    return quarantined_run_id is not None


async def fetch_dispatchable_runs(
    pool: asyncpg.Pool,
    *,
    limit: int = RUN_DISPATCH_BATCH_SIZE,
) -> list[DispatchableRun]:
    """Load ownerless queue rows; lease acquisition performs the real claim."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT run_id, project_id, autonomy_level, status, phase, config
            FROM agent_runs
            WHERE lease_owner_id IS NULL
              AND (
                  status IN ('started', 'running')
                  OR (phase = 'resuming' AND status IN ('approved', 'rejected'))
              )
            ORDER BY updated_at, started_at, run_id
            LIMIT $1
            """,
            limit,
        )
        dispatchable: list[DispatchableRun] = []
        for row in rows:
            try:
                dispatchable.append(_parse_run(row))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                quarantined = await _quarantine_invalid_run(conn, row, exc)
                if quarantined:
                    logger.error(
                        "Quarantined invalid durable agent run %r: %s",
                        row.get("run_id"),
                        exc,
                    )
                else:
                    logger.info(
                        "Invalid durable agent run %r changed before quarantine",
                        row.get("run_id"),
                    )
    return dispatchable


class RunDispatcher:
    """Own local task references while PostgreSQL owns queue durability."""

    def __init__(self, pool: asyncpg.Pool, vector_store: PgVectorStore) -> None:
        self._pool = pool
        self._vector_store = vector_store
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def inflight_run_ids(self) -> tuple[str, ...]:
        return tuple(self._tasks)

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(run_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[%s] Dispatched supervisor exited unexpectedly", run_id)

    async def poll_once(self) -> tuple[str, ...]:
        scheduled: list[str] = []
        for run in await fetch_dispatchable_runs(self._pool):
            if run.run_id in self._tasks:
                continue
            task = asyncio.create_task(
                run_supervisor(
                    pool=self._pool,
                    vector_store=self._vector_store,
                    run_id=run.run_id,
                    project_id=run.project_id,
                    analysis_types=list(run.analysis_types),
                    time_range_days=run.time_range_days,
                    autonomy_level=run.autonomy_level,
                    resume=run.resume,
                    resume_after_approval=run.resume_after_approval,
                    target_proposal_id=run.target_proposal_id,
                ),
                name=f"agent-run:{run.run_id}",
            )
            self._tasks[run.run_id] = task
            task.add_done_callback(
                lambda completed, run_id=run.run_id: self._task_done(run_id, completed)
            )
            scheduled.append(run.run_id)
        return tuple(scheduled)

    async def stop(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


async def dispatch_runs_forever(
    pool: asyncpg.Pool,
    vector_store: PgVectorStore,
    stop: asyncio.Event,
    *,
    interval_seconds: float = RUN_DISPATCH_INTERVAL_SECONDS,
) -> None:
    """Poll durable work on every replica until application shutdown."""
    dispatcher = RunDispatcher(pool, vector_store)
    try:
        while not stop.is_set():
            try:
                await dispatcher.poll_once()
            except Exception:
                logger.exception("Durable agent run dispatch poll failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
    finally:
        await dispatcher.stop()
