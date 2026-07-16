"""Read-only repository grants plus tenant-owned connection policy endpoints.

Repository authority is established out of band by a trusted operator. Project
credentials can inspect that binding and manage policy, but cannot choose,
replace, or disconnect its GitHub target.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Request

from app.auth import require_project
from app.config import codegen_platform_safety_policy
from app.github.repo_context import fetch_repo_context
from app.github.token_broker import (
    GitHubTokenBroker,
    RepositoryAuthorizationError,
)
from app.models.connection import Connection
from app.profiling import RepoProfile
from app.safety.policy import (
    TenantCodegenConnectionPolicy,
    validate_tenant_policy_against_platform,
)
from app.store import connections as store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/connections",
    tags=["connections"],
)


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


@router.get(
    "/{project_id}/tenant-policy", response_model=TenantCodegenConnectionPolicy
)
async def get_tenant_policy(
    project_id: str, request: Request
) -> TenantCodegenConnectionPolicy:
    """Return the tenant-owned Codegen preferences for a repo binding."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, project_id, "agents:read")
    policy = await store.get_tenant_policy(pool, project_id)
    if policy is None:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        )
    return policy


@router.put(
    "/{project_id}/tenant-policy", response_model=TenantCodegenConnectionPolicy
)
async def replace_tenant_policy(
    project_id: str,
    body: TenantCodegenConnectionPolicy,
    request: Request,
) -> TenantCodegenConnectionPolicy:
    """Completely replace tenant preferences without mutating platform safety."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, project_id, "agents:manage")
    platform_policy = getattr(
        request.app.state,
        "platform_codegen_safety_policy",
        None,
    ) or codegen_platform_safety_policy()
    try:
        validate_tenant_policy_against_platform(body, platform_policy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    policy = await store.replace_tenant_policy(pool, project_id, body)
    if policy is None:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        )
    return policy


@router.get("/{project_id}/repo-context", response_model=RepoProfile)
async def get_repo_context(project_id: str, request: Request) -> RepoProfile:
    """Canonical repository profile for planning and code generation.

    Called by the agents service before proposing features, so proposals are
    grounded in what the connected repository actually is — see
    :mod:`app.github.repo_context`.
    """
    require_project(request, project_id, "agents:read")
    token_broker: GitHubTokenBroker = request.app.state.github_token_broker
    try:
        async with token_broker.read_project(project_id) as (connection, token):
            return await fetch_repo_context(
                repo=connection.repository_full_name,
                branch=connection.default_base_branch,
                token=token,
            )
    except RepositoryAuthorizationError as exc:
        raise HTTPException(
            status_code=404, detail=f"No repo connection for project '{project_id}'."
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "Repo context fetch failed for project %s: %s", project_id, exc
        )
        raise HTTPException(
            status_code=502, detail=f"GitHub repo context fetch failed: {exc}"
        ) from exc
