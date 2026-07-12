"""Repo connection registry endpoints.

Binds an APDL project to a GitHub App installation + repository. Guarded by the
canonical project API key and role checks; browser requests reach them only
through the session-authenticated Admin API.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request

from app.auth import require_project
from app.github.app_auth import mint_token_for_repo, resolve_installation_id
from app.github.repo_context import fetch_repo_context
from app.models.connection import Connection, ConnectionCreate
from app.profiling import RepoProfile
from app.store import connections as store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/connections",
    tags=["connections"],
)


@router.post("", response_model=Connection, status_code=201)
async def create_connection(body: ConnectionCreate, request: Request) -> Connection:
    """Register (or update) the repo binding for a project.

    ``installation_id`` may be omitted: the service resolves the live id from
    the repo slug via the App JWT, which doubles as install validation — a repo
    the App is not installed on fails here with a clear 422 instead of the
    first changeset dying at token-mint later.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, body.project_id, "agents:manage")
    if body.installation_id is None:
        try:
            live_id = await resolve_installation_id(body.repo)
        except ValueError as exc:
            # App ID / private key not configured on this deployment.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"The APDL GitHub App is not installed on '{body.repo}' "
                        "(or the repo does not exist). Install the App on the "
                        "repository, then connect it."
                    ),
                ) from exc
            raise HTTPException(
                status_code=502,
                detail=f"GitHub installation lookup failed: {exc}",
            ) from exc
        body = body.model_copy(update={"installation_id": live_id})
    return await store.upsert_connection(pool, body)


@router.get("/{project_id}", response_model=Connection)
async def get_connection(project_id: str, request: Request) -> Connection:
    """Resolve a project's repo binding."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, project_id, "agents:read")
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
    require_project(request, project_id, "agents:manage")
    deleted = await store.delete_connection(pool, project_id)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        )


@router.get("/{project_id}/repo-context", response_model=RepoProfile)
async def get_repo_context(project_id: str, request: Request) -> RepoProfile:
    """Canonical repository profile for planning and code generation.

    Called by the agents service before proposing features, so proposals are
    grounded in what the connected repository actually is — see
    :mod:`app.github.repo_context`.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, project_id, "agents:read")
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
