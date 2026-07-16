"""Trigger endpoint — start an agent run."""

from __future__ import annotations

import json
import logging
import uuid
from enum import Enum

import asyncpg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

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
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    trigger_type: TriggerType
    analysis_types: list[str] = Field(
        default_factory=lambda: ["behavior_analysis"],
        min_length=1,
        max_length=16,
        description="Agent graphs to run; disabled built-ins are rejected.",
    )
    time_range_days: int = Field(default=7, ge=1, le=90)
    autonomy_level: int = Field(
        default=2,
        ge=1,
        le=4,
        description=(
            "L1=suggest only; L2=approval; L3/L4 may auto-apply eligible actions "
            "only when the operator explicitly enables autonomous mutations; "
            "inherently gated actions always require approval"
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

    run_id = str(uuid.uuid4())

    # One pipeline per project at a time: concurrent runs duplicate LLM spend
    # and can deploy duplicate experiments from the same insight. Serialize the
    # check + insert with a transaction-scoped, project-keyed advisory lock so
    # concurrent requests on different service replicas cannot both observe an
    # empty slot. Runs parked at waiting_approval do not block because they are
    # not executing.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                SELECT pg_advisory_xact_lock(
                    hashtextextended('apdl:agent-run:' || $1::text, 0)
                )
                """,
                body.project_id,
            )
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
                    detail=(
                        f"Project {body.project_id} already has an active run ({active})."
                    ),
                )

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
    )

    return TriggerResponse(run_id=run_id, status="started")
