"""Changeset lifecycle endpoints.

Phase 1 scope: create (enqueue), read, list, and abandon. The sandboxed job
that drives ``queued → … → merged`` is wired in later phases; the seam is
:func:`_enqueue_job`. Merge (``POST /{id}/merge``) arrives with CI gating in a
later phase and is intentionally absent here rather than stubbed to lie.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import require_internal_token
from app.models.changeset import Changeset, ChangesetCreate, ChangesetStatus, InvalidTransition
from app.store import changesets as store
from app.store import connections as connections_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/changesets",
    tags=["changesets"],
    dependencies=[Depends(require_internal_token)],
)


def _enqueue_job(changeset_id: str) -> None:
    """Seam for the sandboxed code job (Phase 2+). Currently a no-op."""
    logger.info(
        "Changeset %s queued; sandbox job runner not yet wired (Phase 2).", changeset_id
    )


@router.post("", response_model=Changeset, status_code=202)
async def create_changeset(body: ChangesetCreate, request: Request) -> Changeset:
    """Enqueue a changeset for a connected project."""
    pool: asyncpg.Pool = request.app.state.pg_pool

    connection = await connections_store.get_connection(pool, body.project_id)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project_id}' has no connected repository.",
        )

    changeset_id = f"cs_{uuid.uuid4().hex[:24]}"
    base_branch = body.base_branch or connection.default_base_branch
    changeset = await store.create_changeset(
        pool,
        changeset_id=changeset_id,
        project_id=body.project_id,
        run_id=body.run_id,
        base_branch=base_branch,
        task=body.task.model_dump(),
    )
    _enqueue_job(changeset_id)
    return changeset


@router.get("", response_model=list[Changeset])
async def list_changesets(
    request: Request,
    project_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
) -> list[Changeset]:
    """List a project's changesets, most recent first."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    return await store.list_changesets(pool, project_id, limit)


@router.get("/{changeset_id}", response_model=Changeset)
async def get_changeset(changeset_id: str, request: Request) -> Changeset:
    """Fetch one changeset by id."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    changeset = await store.get_changeset(pool, changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    return changeset


@router.post("/{changeset_id}/abandon", response_model=Changeset)
async def abandon_changeset(changeset_id: str, request: Request) -> Changeset:
    """Abandon a changeset (rollback for an un-merged change)."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    try:
        changeset = await store.transition_changeset(
            pool, changeset_id, ChangesetStatus.abandoned
        )
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    return changeset
