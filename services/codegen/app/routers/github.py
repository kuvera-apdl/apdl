"""GitHub App introspection endpoints.

Read-only views of what the App can reach on github.com, used by the admin
console to turn "connect a repo" into a picker instead of hand-typed slugs and
installation ids. Access is scoped by the canonical project API key and role.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.auth import require_project
from app.github.installations import AccessibleRepo, list_accessible_repos
from app.store import connections as connections_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/github",
    tags=["github"],
)


@router.get("/repos", response_model=list[AccessibleRepo])
async def get_accessible_repos(
    request: Request, project_id: str = Query(...)
) -> list[AccessibleRepo]:
    """Return only the repository already bound to the authorized project."""
    require_project(request, project_id, "agents:read")
    pool: asyncpg.Pool = request.app.state.pg_pool
    connection = await connections_store.get_connection(pool, project_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Project has no repository connection.")
    try:
        repositories = await list_accessible_repos()
        return [repo for repo in repositories if repo.repo == connection.repo]
    except ValueError as exc:
        # build_app_jwt: App ID / private key not configured on this deployment.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.warning("GitHub repo listing failed: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"GitHub repository listing failed: {exc}"
        ) from exc
