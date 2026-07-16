"""Trigger endpoint — start an agent run."""

from __future__ import annotations

import json
import logging
import uuid
from enum import Enum

import asyncpg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_project
from app.framework.registry import is_registered, registered_agents
from app.graphs.supervisor import run_supervisor
from app.store.custom_agents import fetch_active_by_slugs
from app.store.run_leases import RUN_LEASE_SECONDS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])


class TriggerType(str, Enum):
    scheduled = "scheduled"
    manual = "manual"
    threshold_alert = "threshold_alert"


class TriggerRequest(BaseModel):
    project_id: str = Field(min_length=1)
    trigger_type: TriggerType
    analysis_types: list[str] = Field(
        default_factory=lambda: ["behavior_analysis"],
        min_length=1,
        max_length=16,
        description="Agent graphs to run: behavior_analysis, experiment_design, experiment_evaluation, feature_proposal, code_implementation",
    )
    time_range_days: int = Field(default=7, ge=1, le=90)
    autonomy_level: int = Field(
        default=2,
        ge=1,
        le=4,
        description="L1=suggest only, L2=auto-safe, L3=auto+approve risky, L4=full auto",
    )
    target_experiment_id: str | None = Field(
        default=None,
        description=(
            "Scope an experiment_evaluation run to one experiment (a human's "
            "'evaluate now'); an immature experiment then gets an explicit "
            "immature verdict instead of being skipped."
        ),
    )


class TriggerResponse(BaseModel):
    run_id: str
    status: str


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_agent_run(
    body: TriggerRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> TriggerResponse:
    """Start a new agent run.

    Creates a run record in PostgreSQL and launches the supervisor graph
    as a background task.
    """
    require_project(request, body.project_id, "agents:run")
    pool: asyncpg.Pool = request.app.state.pg_pool

    # Names not in the built-in registry may still be the project's custom
    # agents — resolve those before rejecting. Reject up front — otherwise
    # the caller gets a 200 "started" for a run the supervisor will only
    # skip through.
    unknown = sorted({t for t in body.analysis_types if not is_registered(t)})
    if unknown:
        custom = await fetch_active_by_slugs(pool, body.project_id, unknown)
        still_unknown = sorted(set(unknown) - set(custom))
        if still_unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown analysis_types: {still_unknown}",
            )

    # Disabled built-ins are registered but not runnable (e.g. personalization
    # while its delivery path doesn't exist) — reject like unknown names rather
    # than accepting a run the supervisor would only skip through.
    builtin = registered_agents()
    disabled = sorted(
        {
            t
            for t in body.analysis_types
            if t in builtin and not getattr(builtin[t], "enabled", True)
        }
    )
    if disabled:
        raise HTTPException(
            status_code=422,
            detail=f"Disabled analysis_types: {disabled}",
        )

    # One pipeline per project at a time: concurrent runs duplicate LLM spend
    # and can deploy duplicate experiments from the same insight. (Runs parked
    # at waiting_approval don't block — they're not executing.)
    async with pool.acquire() as conn:
        active = await conn.fetchval(
            """
            SELECT run_id FROM agent_runs
            WHERE project_id = $1
              AND (status IN ('started', 'running')
                   OR (phase = 'resuming' AND status IN ('approved', 'rejected')))
            LIMIT 1
            """,
            body.project_id,
        )
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Project {body.project_id} already has an active run ({active}).",
        )

    run_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_runs
                (run_id, project_id, trigger_type, autonomy_level, status, phase,
                 lease_expires_at, config)
            VALUES ($1, $2, $3, $4, 'started', 'initializing',
                    now() + ($5 * interval '1 second'), $6::jsonb)
            """,
            run_id,
            body.project_id,
            body.trigger_type.value,
            body.autonomy_level,
            RUN_LEASE_SECONDS,
            json.dumps(
                {
                    "analysis_types": body.analysis_types,
                    "time_range_days": body.time_range_days,
                    **(
                        {"target_experiment_id": body.target_experiment_id}
                        if body.target_experiment_id
                        else {}
                    ),
                }
            ),
        )

    logger.info("Agent run %s created for project %s", run_id, body.project_id)

    # Launch supervisor in background
    background_tasks.add_task(
        run_supervisor,
        pool=pool,
        vector_store=request.app.state.vector_store,
        run_id=run_id,
        project_id=body.project_id,
        analysis_types=body.analysis_types,
        time_range_days=body.time_range_days,
        autonomy_level=body.autonomy_level,
        target_experiment_id=body.target_experiment_id,
    )

    return TriggerResponse(run_id=run_id, status="started")
