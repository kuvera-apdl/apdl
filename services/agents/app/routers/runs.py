"""Run introspection endpoints — list, results, and audit.

Closes the admin-console plan's §8 gaps that made agent runs opaque over
HTTP: G1 (no way to list runs), G3 (per-agent outputs lived only in the
in-process state dict, leaving approvals blind), and G2 (the audit trail was
queryable only in-process via AuditLogger).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth import require_project, require_role
from app.routers.status import RUN_STATUS_COLUMNS, RunStatus, row_to_status
from app.safety.audit import AuditLogger
from app.store.run_leases import (
    RunCancellationConflictError,
    RunCancellationNotFoundError,
    cancel_run,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["agents"])

RESULT_KEYS = (
    "insights",
    "experiment_designs",
    "personalizations",
    "feature_proposals",
    "changesets",
)


class RunListResponse(BaseModel):
    runs: list[RunStatus]
    count: int


class RunResults(BaseModel):
    run_id: str
    insights: list[Any]
    experiment_designs: list[Any]
    personalizations: list[Any]
    feature_proposals: list[Any]
    changesets: list[Any]
    #: Outputs of user-defined custom agents, keyed by their ``produces``
    #: (validation guarantees those never collide with the fixed keys above).
    custom_outputs: dict[str, list[Any]] = {}


class RunAuditResponse(BaseModel):
    run_id: str
    audit: list[dict[str, Any]]
    count: int


class RunCancellationResponse(BaseModel):
    run_id: str
    previous_status: str
    status: Literal["cancelled", "cancelling"]


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    project_id: str = Query(..., min_length=1),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> RunListResponse:
    """List agent runs for a project, newest first (gap G1)."""
    require_project(request, project_id, "agents:read")
    pool: asyncpg.Pool = request.app.state.pg_pool

    base = f"SELECT {RUN_STATUS_COLUMNS} FROM agent_runs WHERE project_id = $1"
    async with pool.acquire() as conn:
        if status is not None:
            rows = await conn.fetch(
                base + " AND status = $2 ORDER BY started_at DESC LIMIT $3",
                project_id,
                status,
                limit,
            )
        else:
            rows = await conn.fetch(
                base + " ORDER BY started_at DESC LIMIT $2",
                project_id,
                limit,
            )

    runs = [row_to_status(row) for row in rows]
    return RunListResponse(runs=runs, count=len(runs))


async def _require_run(pool: asyncpg.Pool, run_id: str, project_id: str) -> None:
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM agent_runs WHERE run_id = $1 AND project_id = $2",
            run_id,
            project_id,
        )
    if exists is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")


@router.post("/{run_id}/cancel", response_model=RunCancellationResponse)
async def cancel_agent_run(run_id: str, request: Request) -> RunCancellationResponse:
    """Fence new work and retain the project lane while claimed effects drain."""
    principal = require_role(request, "agents:run")
    pool: asyncpg.Pool = request.app.state.pg_pool
    try:
        result = await cancel_run(
            pool,
            run_id=run_id,
            project_id=principal.project_id,
            actor_credential_id=principal.credential_id,
            actor_user_id=principal.actor_user_id,
        )
    except RunCancellationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunCancellationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunCancellationResponse(
        run_id=result.run_id,
        previous_status=result.previous_status,
        status=result.status,
    )


@router.get("/{run_id}/results", response_model=RunResults)
async def get_run_results(run_id: str, request: Request) -> RunResults:
    """Per-agent outputs persisted at phase completion (gap G3).

    Keys with no persisted output (agent skipped, still running, or the run
    predates result persistence) are empty lists.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    principal = require_role(request, "agents:read")
    await _require_run(pool, run_id, principal.project_id)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT produces, output FROM agent_run_results WHERE run_id = $1",
            run_id,
        )

    payload: dict[str, list[Any]] = {key: [] for key in RESULT_KEYS}
    custom_outputs: dict[str, list[Any]] = {}
    for row in rows:
        output = row["output"]
        if isinstance(output, str):
            # One malformed row must not 500 the whole endpoint.
            try:
                output = json.loads(output)
            except (json.JSONDecodeError, ValueError):
                logger.error(
                    "[%s] Skipping malformed %s result row", run_id, row["produces"]
                )
                continue
        if not isinstance(output, list):
            continue
        if row["produces"] in payload:
            # Extend, don't overwrite — two agents can persist under the same
            # produces key (PK is run_id+agent_name), and the approval path
            # already merges across rows.
            payload[row["produces"]].extend(output)
        else:
            # A custom agent's produces key — surface it instead of dropping it.
            custom_outputs.setdefault(row["produces"], []).extend(output)

    return RunResults(run_id=run_id, custom_outputs=custom_outputs, **payload)


@router.get("/{run_id}/audit", response_model=RunAuditResponse)
async def get_run_audit(
    run_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> RunAuditResponse:
    """The run's audit trail over HTTP, newest first (gap G2)."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    principal = require_role(request, "agents:read")
    await _require_run(pool, run_id, principal.project_id)

    entries = await AuditLogger(pool).get_run_audit_trail(run_id, limit=limit)
    return RunAuditResponse(run_id=run_id, audit=entries, count=len(entries))
