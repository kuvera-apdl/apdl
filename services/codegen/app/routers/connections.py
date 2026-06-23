"""Repo connection registry endpoints.

Binds an APDL project to a GitHub App installation + repository. Guarded by the
internal token — these endpoints are called by operators/onboarding, not the
browser.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import require_internal_token
from app.models.connection import Connection, ConnectionCreate
from app.store import connections as store

router = APIRouter(
    prefix="/v1/connections",
    tags=["connections"],
    dependencies=[Depends(require_internal_token)],
)


@router.post("", response_model=Connection, status_code=201)
async def create_connection(body: ConnectionCreate, request: Request) -> Connection:
    """Register (or update) the repo binding for a project."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    return await store.upsert_connection(pool, body)


@router.get("/{project_id}", response_model=Connection)
async def get_connection(project_id: str, request: Request) -> Connection:
    """Resolve a project's repo binding."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    connection = await store.get_connection(pool, project_id)
    if connection is None:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        )
    return connection
