"""Authenticated project creation and profile association."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.auth import AdminSession, require_csrf, require_session
from app.models import (
    AuditCursor,
    AuditPageQuery,
    ExecutionAuthorizationSummary,
    HumanProjectOwnership,
    OperatorManagedProjectOwnership,
    OwnershipAuditEntry,
    OwnershipAuditPage,
    OwnershipTransferRequest,
    ProjectAccess,
    ProjectAuthorizationSummary,
    ProjectCreateRequest,
    ProjectCreator,
    UserIdentity,
)
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
    "members:manage",
)


def _authorization_summary(row) -> ProjectAuthorizationSummary:
    owner_user_id = row["owner_user_id"]
    creator = (
        ProjectCreator(
            user_id=row["created_by"],
            email=str(row["creator_email"]),
        )
        if row["created_by"] is not None
        else None
    )
    ownership = (
        HumanProjectOwnership(
            owner_user_id=owner_user_id,
            owner_email=str(row["owner_email"]),
        )
        if owner_user_id is not None
        else OperatorManagedProjectOwnership()
    )
    source = (
        str(row["execution_authorization_source"])
        if row["execution_authorization_source"] is not None
        else None
    )
    return ProjectAuthorizationSummary(
        project_id=str(row["project_id"]),
        creator=creator,
        ownership=ownership,
        execution_authorization=ExecutionAuthorizationSummary(
            authorized=source is not None,
            source=source,
        ),
    )


async def _fetch_authorization_summary(
    conn,
    *,
    project_id: str,
    actor_user_id: uuid.UUID,
) -> ProjectAuthorizationSummary:
    row = await conn.fetchrow(
        """
        SELECT
            project.project_id,
            project.created_by,
            project.owner_user_id,
            creator.email AS creator_email,
            owner.email AS owner_email,
            execution.authorization_source AS execution_authorization_source
        FROM admin_projects AS project
        JOIN admin_user_projects AS actor_membership
          ON actor_membership.project_id = project.project_id
         AND actor_membership.user_id = $2
        JOIN admin_users AS actor
          ON actor.user_id = actor_membership.user_id
         AND actor.active
        LEFT JOIN admin_users AS creator
          ON creator.user_id = project.created_by
        LEFT JOIN admin_users AS owner
          ON owner.user_id = project.owner_user_id
        LEFT JOIN admin_project_execution_authorizations AS execution
          ON execution.project_id = project.project_id
        WHERE project.project_id = $1
        """,
        project_id,
        actor_user_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Project membership required",
        )
    return _authorization_summary(row)


@router.post("", response_model=UserIdentity, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreateRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> UserIdentity | Response:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            user = await conn.fetchrow(
                """
                SELECT active
                FROM admin_users
                WHERE user_id = $1
                FOR UPDATE
                """,
                uuid.UUID(session.user_id),
            )
            if user is None or not user["active"]:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Account is no longer active",
                )
            project_count = int(
                await conn.fetchval(
                    """
                    SELECT count(*)
                    FROM admin_projects
                    WHERE created_by = $1
                    """,
                    uuid.UUID(session.user_id),
                )
            )
            if project_count >= settings.max_projects_per_user:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT,
                    content={
                        "error": "project_quota_reached",
                        "message": "This account has reached its project creation limit",
                    },
                )
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
            await conn.execute(
                """
                UPDATE admin_projects
                SET owner_user_id = $2
                WHERE project_id = $1
                  AND owner_user_id IS NULL
                """,
                project_id,
                uuid.UUID(session.user_id),
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


@router.get(
    "/{project_id}/authorization",
    response_model=ProjectAuthorizationSummary,
)
async def project_authorization(
    project_id: str,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ProjectAuthorizationSummary:
    async with request.app.state.pg_pool.acquire() as conn:
        return await _fetch_authorization_summary(
            conn,
            project_id=project_id,
            actor_user_id=uuid.UUID(session.user_id),
        )


@router.post(
    "/{project_id}/ownership/transfer",
    response_model=ProjectAuthorizationSummary,
)
async def transfer_project_ownership(
    project_id: str,
    body: OwnershipTransferRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ProjectAuthorizationSummary:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)

    actor_user_id = uuid.UUID(session.user_id)
    if body.target_user_id == actor_user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target user already owns the project",
        )

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            project = await conn.fetchrow(
                """
                SELECT project.owner_user_id
                FROM admin_projects AS project
                JOIN admin_user_projects AS actor_membership
                  ON actor_membership.project_id = project.project_id
                 AND actor_membership.user_id = $2
                JOIN admin_users AS actor
                  ON actor.user_id = actor_membership.user_id
                 AND actor.active
                WHERE project.project_id = $1
                FOR UPDATE OF project
                """,
                project_id,
                actor_user_id,
            )
            if project is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Project membership required",
                )
            if project["owner_user_id"] is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Operator-managed ownership cannot be claimed in the console",
                )
            if project["owner_user_id"] != actor_user_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only the current owner can transfer ownership",
                )

            memberships = await conn.fetch(
                """
                SELECT membership.user_id, membership.roles, account.active
                FROM admin_user_projects AS membership
                JOIN admin_users AS account
                  ON account.user_id = membership.user_id
                WHERE membership.project_id = $1
                  AND membership.user_id = ANY($2::UUID[])
                ORDER BY membership.user_id
                FOR UPDATE OF membership, account
                """,
                project_id,
                [actor_user_id, body.target_user_id],
            )
            by_user_id = {row["user_id"]: row for row in memberships}
            target = by_user_id.get(body.target_user_id)
            if (
                target is None
                or not target["active"]
                or "members:manage" not in target["roles"]
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Target must be an active project member with members:manage"
                    ),
                )

            result = await conn.execute(
                """
                UPDATE admin_projects
                SET owner_user_id = $3
                WHERE project_id = $1
                  AND owner_user_id = $2
                """,
                project_id,
                actor_user_id,
                body.target_user_id,
            )
            if result != "UPDATE 1":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Project ownership changed; refresh and try again",
                )
            await conn.execute(
                """
                INSERT INTO admin_project_ownership_audit (
                    audit_id,
                    project_id,
                    previous_owner_user_id,
                    new_owner_user_id,
                    actor,
                    reason
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                uuid.uuid4(),
                project_id,
                actor_user_id,
                body.target_user_id,
                session.email,
                body.reason or "No reason provided",
            )

        return await _fetch_authorization_summary(
            conn,
            project_id=project_id,
            actor_user_id=actor_user_id,
        )


@router.get(
    "/{project_id}/ownership/audit",
    response_model=OwnershipAuditPage,
)
async def project_ownership_audit(
    project_id: str,
    request: Request,
    page: Annotated[AuditPageQuery, Query()],
    session: AdminSession = Depends(require_session),
) -> OwnershipAuditPage:
    async with request.app.state.pg_pool.acquire() as conn:
        authorized = await conn.fetchval(
            """
            SELECT membership.user_id
            FROM admin_user_projects AS membership
            JOIN admin_users AS account
              ON account.user_id = membership.user_id
             AND account.active
            WHERE membership.project_id = $1
              AND membership.user_id = $2
              AND 'members:manage' = ANY(membership.roles)
            """,
            project_id,
            uuid.UUID(session.user_id),
        )
        if authorized is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Current members:manage authority is required",
            )
        rows = await conn.fetch(
            """
            SELECT
                audit.audit_id,
                audit.project_id,
                audit.previous_owner_user_id,
                previous_owner.email AS previous_owner_email,
                audit.new_owner_user_id,
                new_owner.email AS new_owner_email,
                audit.actor,
                audit.reason,
                audit.created_at
            FROM admin_project_ownership_audit AS audit
            LEFT JOIN admin_users AS previous_owner
              ON previous_owner.user_id = audit.previous_owner_user_id
            JOIN admin_users AS new_owner
              ON new_owner.user_id = audit.new_owner_user_id
            WHERE audit.project_id = $1
              AND (
                  $2::TIMESTAMPTZ IS NULL
                  OR (audit.created_at, audit.audit_id)
                     < ($2::TIMESTAMPTZ, $3::UUID)
              )
            ORDER BY audit.created_at DESC, audit.audit_id DESC
            LIMIT $4
            """,
            project_id,
            page.before_created_at,
            page.before_audit_id,
            page.limit + 1,
        )
    page_rows = rows[: page.limit]
    entries = [OwnershipAuditEntry(**dict(row)) for row in page_rows]
    next_cursor = (
        AuditCursor(
            created_at=page_rows[-1]["created_at"],
            audit_id=page_rows[-1]["audit_id"],
        )
        if len(rows) > page.limit
        else None
    )
    return OwnershipAuditPage(entries=entries, next_cursor=next_cursor)
