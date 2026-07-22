"""Authenticated, tenant-scoped Codegen capability routes."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Query, Request

from app.auth import require_project
from app.capabilities import ChangesetCreationCapability, evaluate_changeset_creation

router = APIRouter(prefix="/v1/capabilities", tags=["capabilities"])


@router.get("/changeset-creation", response_model=ChangesetCreationCapability)
async def changeset_creation_capability(
    request: Request,
    project_id: str = Query(..., pattern=r"^[A-Za-z0-9]{1,64}$"),
) -> ChangesetCreationCapability:
    """Return executable changeset authority for exactly the caller's project."""
    require_project(request, project_id, "agents:manage")
    pool: asyncpg.Pool = request.app.state.pg_pool
    evaluation = await evaluate_changeset_creation(request.app, pool, project_id)
    return evaluation.report
