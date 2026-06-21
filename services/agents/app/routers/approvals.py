"""Approval endpoint — approve or reject a pending agent action."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from app.graphs.supervisor import run_supervisor
from app.store.proposals import enqueue_proposals

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])


class ApprovalRequest(BaseModel):
    approved: bool
    comment: str | None = None


class ApprovalResponse(BaseModel):
    run_id: str
    status: str
    message: str


@router.post("/{run_id}/approve", response_model=ApprovalResponse)
async def approve_action(
    run_id: str,
    body: ApprovalRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> ApprovalResponse:
    """Approve or reject a pending agent action.

    When an agent run reaches an approval gate (human-in-the-loop interrupt),
    this endpoint records the decision and updates the run status so the
    supervisor can resume.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool

    async with pool.acquire() as conn:
        row: Any = await conn.fetchrow(
            "SELECT run_id, status, phase, project_id, autonomy_level "
            "FROM agent_runs WHERE run_id = $1",
            run_id,
        )

        if row is None:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

        if row["status"] != "waiting_approval":
            raise HTTPException(
                status_code=400,
                detail=f"Run {run_id} is not waiting for approval (current status: {row['status']})",
            )

        new_status = "approved" if body.approved else "rejected"
        new_phase = "resuming" if body.approved else "completed"

        await conn.execute(
            """
            UPDATE agent_runs
            SET status = $2, phase = $3, updated_at = now()
            WHERE run_id = $1
            """,
            run_id,
            new_status,
            new_phase,
        )

        # Record in audit log
        await conn.execute(
            """
            INSERT INTO agent_audit_log (run_id, action_type, config, approval_status)
            VALUES ($1, 'human_approval', $2, $3)
            """,
            run_id,
            {"comment": body.comment},
            new_status,
        )

    if body.approved:
        await _kick_code_implementation(
            request.app,
            background_tasks,
            run_id=run_id,
            project_id=row["project_id"],
            autonomy_level=row["autonomy_level"],
        )

    action_word = "approved" if body.approved else "rejected"
    logger.info("Run %s %s by human reviewer", run_id, action_word)

    return ApprovalResponse(
        run_id=run_id,
        status=new_status,
        message=f"Action {action_word} successfully.",
    )


async def _kick_code_implementation(
    app: Any,
    background_tasks: BackgroundTasks,
    *,
    run_id: str,
    project_id: str,
    autonomy_level: int,
) -> None:
    """Enqueue the run's approved proposals and kick a code_implementation run.

    Decision D2 (hybrid): the durable ``feature_proposals`` queue is the source
    of truth; approval enqueues into it AND opportunistically kicks a run. The
    drain is claim-based, so the kick and any scheduled sweep are idempotent.
    Best-effort — a kick failure must never break the approval response.
    """
    pool: asyncpg.Pool = app.state.pg_pool
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT output FROM agent_run_results
                WHERE run_id = $1 AND produces = 'feature_proposals'
                """,
                run_id,
            )

        proposals: list[dict[str, Any]] = []
        for record in rows:
            output = record["output"]
            data = json.loads(output) if isinstance(output, str) else output
            if isinstance(data, list):
                proposals.extend(p for p in data if isinstance(p, dict))
        if not proposals:
            return

        await enqueue_proposals(pool, run_id, project_id, proposals)

        new_run_id = str(uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_runs
                    (run_id, project_id, trigger_type, autonomy_level, status, phase, config)
                VALUES ($1, $2, 'manual', $3, 'started', 'initializing', $4::jsonb)
                """,
                new_run_id,
                project_id,
                autonomy_level,
                json.dumps({"analysis_types": ["code_implementation"], "kicked_by": run_id}),
            )

        background_tasks.add_task(
            run_supervisor,
            pool=pool,
            vector_store=app.state.vector_store,
            run_id=new_run_id,
            project_id=project_id,
            analysis_types=["code_implementation"],
            time_range_days=7,
            autonomy_level=autonomy_level,
        )
        logger.info(
            "Kicked code_implementation run %s from approved run %s", new_run_id, run_id
        )
    except Exception:
        logger.exception("Failed to kick code_implementation from run %s", run_id)
