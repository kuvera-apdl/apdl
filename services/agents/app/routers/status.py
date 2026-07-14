"""Status endpoint — check agent run progress."""

from __future__ import annotations

import json
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
    #: The run's trigger inputs, surfaced so clients don't need to remember
    #: what they requested (the console previously cached this in localStorage).
    #: Defaults keep the model constructible from an older row shape.
    trigger_type: str = ""
    autonomy_level: int | None = None
    analysis_types: list[str] = []


def _analysis_types_from_config(raw: Any) -> list[str]:
    """The requested agents from agent_runs.config (JSONB, sometimes a str)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw, dict):
        return []
    types = raw.get("analysis_types")
    return [str(t) for t in types] if isinstance(types, list) else []


#: Columns every run→RunStatus mapping needs; shared by the status and list
#: endpoints so they return an identical shape.
RUN_STATUS_COLUMNS = (
    "run_id, project_id, status, phase, insights_count, experiments_count, "
    "started_at, updated_at, trigger_type, autonomy_level, config"
)


def row_to_status(row: Any) -> RunStatus:
    return RunStatus(
        run_id=row["run_id"],
        project_id=row["project_id"],
        status=row["status"],
        phase=row["phase"] or "initializing",
        insights_count=row["insights_count"],
        experiments_count=row["experiments_count"],
        started_at=row["started_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        trigger_type=row["trigger_type"] or "",
        autonomy_level=row["autonomy_level"],
        analysis_types=_analysis_types_from_config(row["config"]),
    )


@router.get("/{run_id}/status", response_model=RunStatus)
async def get_run_status(run_id: str, request: Request) -> RunStatus:
    """Retrieve the current state of an agent run."""
    pool: asyncpg.Pool = request.app.state.pg_pool

    async with pool.acquire() as conn:
        row: Any = await conn.fetchrow(
            f"SELECT {RUN_STATUS_COLUMNS} FROM agent_runs WHERE run_id = $1",
            run_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return row_to_status(row)
