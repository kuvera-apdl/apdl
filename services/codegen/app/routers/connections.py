"""Repo connection registry endpoints.

Binds an APDL project to a GitHub App installation + repository. Guarded by the
internal token — these endpoints are called by operators/onboarding (and the
agents service, for repo context), not the browser.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import require_internal_token
from app.github.app_auth import mint_token_for_repo
from app.github.repo_context import fetch_repo_context
from app.models.connection import Connection, ConnectionCreate
from app.store import connections as store

logger = logging.getLogger(__name__)

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


@router.delete("/{project_id}", status_code=204)
async def delete_connection(project_id: str, request: Request) -> None:
    """Disconnect a project from its repository.

    Removes the binding only — the GitHub App installation itself is managed on
    github.com and is untouched, as are existing changesets and open PRs.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    deleted = await store.delete_connection(pool, project_id)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        )


@router.get("/{project_id}/repo-context")
async def get_repo_context(project_id: str, request: Request) -> dict:
    """Compact repo facts (stack, layout, scripts, README) for planning agents.

    Called by the agents service before proposing features, so proposals are
    grounded in what the connected repository actually is — see
    :mod:`app.github.repo_context`.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    connection = await store.get_connection(pool, project_id)
    if connection is None:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        )
    try:
        token = await mint_token_for_repo(connection.installation_id, connection.repo)
        return await fetch_repo_context(
            repo=connection.repo,
            branch=connection.default_base_branch,
            token=token.token,
        )
    except httpx.HTTPError as exc:
        logger.warning("Repo context fetch failed for %s: %s", connection.repo, exc)
        raise HTTPException(
            status_code=502, detail=f"GitHub repo context fetch failed: {exc}"
        ) from exc
