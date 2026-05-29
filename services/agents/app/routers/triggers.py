"""Trigger endpoint — start an agent run."""

from __future__ import annotations

import logging
import uuid
from enum import Enum

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel, Field

from app.graphs.supervisor import run_supervisor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])


class TriggerType(str, Enum):
    scheduled = "scheduled"
    manual = "manual"
    threshold_alert = "threshold_alert"


class TriggerRequest(BaseModel):
    project_id: str
    trigger_type: TriggerType
    analysis_types: list[str] = Field(
        default_factory=lambda: ["behavior_analysis"],
        description="Agent graphs to run: behavior_analysis, experiment_design, personalization, feature_proposal",
    )
    time_range_days: int = Field(default=7, ge=1, le=90)
    autonomy_level: int = Field(
        default=2,
        ge=1,
        le=4,
        description="L1=suggest only, L2=auto-safe, L3=auto+approve risky, L4=full auto",
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
    pool: asyncpg.Pool = request.app.state.pg_pool

    run_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_runs (run_id, project_id, trigger_type, autonomy_level, status, phase, config)
            VALUES ($1, $2, $3, $4, 'started', 'initializing', $5)
            """,
            run_id,
            body.project_id,
            body.trigger_type.value,
            body.autonomy_level,
            {
                "analysis_types": body.analysis_types,
                "time_range_days": body.time_range_days,
            },
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
