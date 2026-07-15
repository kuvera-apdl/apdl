"""Status endpoint — check agent run progress."""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])


class RunStatus(BaseModel):
    run_id: str
    project_id: str
    status: str
    phase: str
    insights_count: int
    experiments_count: int
    started_at: str
    updated_at: str


@router.get("/{run_id}/status", response_model=RunStatus)
async def get_run_status(run_id: str, request: Request) -> RunStatus:
    """Retrieve the current state of an agent run."""
    pool: asyncpg.Pool = request.app.state.pg_pool

    async with pool.acquire() as conn:
        row: Any = await conn.fetchrow(
            """
            SELECT run_id, project_id, status, phase, insights_count,
                   experiments_count, started_at, updated_at
            FROM agent_runs
            WHERE run_id = $1
            """,
            run_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return RunStatus(
        run_id=row["run_id"],
        project_id=row["project_id"],
        status=row["status"],
        phase=row["phase"] or "initializing",
        insights_count=row["insights_count"],
        experiments_count=row["experiments_count"],
        started_at=row["started_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )
