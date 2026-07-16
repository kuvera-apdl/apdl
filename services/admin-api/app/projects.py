"""Authenticated project creation and profile association."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import AdminSession, require_csrf, require_session
from app.models import ProjectAccess, ProjectCreateRequest, UserIdentity
from app.security import require_allowed_origin

router = APIRouter(prefix="/api/projects", tags=["projects"])

PROJECT_CREATOR_ROLES = (
    "events:write",
    "config:read",
    "config:write",
    "config:evaluate",
    "query:read",
    "agents:read",
    "credentials:manage",
)


@router.post("", response_model=UserIdentity, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> UserIdentity:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            project_id = await conn.fetchval(
                """
                INSERT INTO admin_projects (project_id, created_by)
                VALUES ($1, $2)
                ON CONFLICT (project_id) DO NOTHING
                RETURNING project_id
                """,
                body.project_id,
                uuid.UUID(session.user_id),
            )
            if project_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Project ID already exists",
                )
            await conn.execute(
                """
                INSERT INTO admin_user_projects (user_id, project_id, roles)
                VALUES ($1, $2, $3)
                """,
                uuid.UUID(session.user_id),
                project_id,
                list(PROJECT_CREATOR_ROLES),
            )

    projects = dict(session.projects)
    projects[str(project_id)] = frozenset(PROJECT_CREATOR_ROLES)
    return UserIdentity(
        user_id=session.user_id,
        email=session.email,
        projects=[
            ProjectAccess(project_id=item_id, roles=sorted(roles))
            for item_id, roles in sorted(projects.items())
        ],
    )
