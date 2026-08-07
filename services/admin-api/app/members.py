"""Secure project invitations and live-authority membership management."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.auth import (
    ACCOUNT_REGISTRATION_LOCK_ID,
    AdminSession,
    _start_session,
    require_csrf,
    require_session,
)
from app.login_security import (
    LoginSource,
    build_login_source,
    preflight_invitation_rate_limit,
    set_device_cookie,
)
from app.models import (
    AuditCursor,
    AuditPageQuery,
    InvitationCreateRequest,
    InvitationInspection,
    InvitationRegistrationRequest,
    MemberRolesReplaceRequest,
    MembershipAuditEntry,
    MembershipAuditPage,
    PendingProjectInvitation,
    ProjectAccess,
    ProjectInvitationReveal,
    ProjectMember,
    ProjectMembers,
    UserIdentity,
)
from app.security import (
    hash_password,
    new_token,
    require_allowed_origin,
    set_session_cookies,
    token_hash,
)

router = APIRouter(tags=["project members"])
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
MEMBERS_MANAGE_ROLE = "members:manage"


def _unavailable_invitation() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Invitation is unavailable",
    )


async def _rate_limit_invitation(
    conn,
    *,
    request: Request,
    digest: str,
) -> LoginSource:
    settings = request.app.state.settings
    now = datetime.now(timezone.utc)
    source = build_login_source(request, f"invitation:{digest}", settings)
    retry_after = await preflight_invitation_rate_limit(
        conn,
        source,
        settings,
        now,
    )
    if retry_after > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many invitation attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    return source


async def _manager_context(
    conn,
    *,
    project_id: str,
    actor_user_id: uuid.UUID,
    lock: bool,
):
    lock_clause = (
        "FOR UPDATE OF project, membership, account"
        if lock
        else ""
    )
    row = await conn.fetchrow(
        f"""
        SELECT
            membership.roles,
            project.owner_user_id,
            (project.owner_user_id = membership.user_id) AS is_owner
        FROM admin_projects AS project
        JOIN admin_user_projects AS membership
          ON membership.project_id = project.project_id
         AND membership.user_id = $2
        JOIN admin_users AS account
          ON account.user_id = membership.user_id
         AND account.active
        WHERE project.project_id = $1
        {lock_clause}
        """,
        project_id,
        actor_user_id,
    )
    if row is None or MEMBERS_MANAGE_ROLE not in row["roles"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Current members:manage authority is required",
        )
    return row


def _assert_role_grant(
    *,
    actor_roles: list[str],
    actor_is_owner: bool,
    requested_roles: list[str],
    current_roles: list[str] | None = None,
) -> None:
    if not set(requested_roles).issubset(actor_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requested roles exceed the actor's current role ceiling",
        )
    if not actor_is_owner and (
        MEMBERS_MANAGE_ROLE in requested_roles
        or (
            current_roles is not None
            and MEMBERS_MANAGE_ROLE in current_roles
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the project owner can change members:manage grants",
        )


async def _record_membership_audit(
    conn,
    *,
    project_id: str,
    action: str,
    actor_user_id: uuid.UUID,
    subject_email: str,
    subject_user_id: uuid.UUID | None = None,
    invitation_id: uuid.UUID | None = None,
    previous_roles: list[str] | None = None,
    new_roles: list[str] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO admin_project_membership_audit (
            audit_id,
            project_id,
            action,
            actor_user_id,
            subject_user_id,
            subject_email,
            invitation_id,
            previous_roles,
            new_roles
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        uuid.uuid4(),
        project_id,
        action,
        actor_user_id,
        subject_user_id,
        subject_email,
        invitation_id,
        previous_roles,
        new_roles,
    )


def _member(row) -> ProjectMember:
    return ProjectMember(
        user_id=row["user_id"],
        email=str(row["email"]),
        roles=[str(role) for role in row["roles"]],
        active=bool(row["active"]),
        is_owner=bool(row["is_owner"]),
        joined_at=row["joined_at"],
    )


def _invitation(row) -> PendingProjectInvitation:
    blocked_reason = row["blocked_reason"]
    return PendingProjectInvitation(
        invitation_id=row["invitation_id"],
        email=str(row["email"]),
        roles=[str(role) for role in row["roles"]],
        inviter_email=str(row["inviter_email"]),
        status="valid" if blocked_reason is None else "blocked",
        blocked_reason=blocked_reason,
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


async def _valid_invitation(conn, *, digest: str, lock: bool):
    lock_clause = (
        "FOR UPDATE OF invitation, project, inviter_account, inviter_membership"
        if lock
        else ""
    )
    return await conn.fetchrow(
        f"""
        SELECT
            invitation.invitation_id,
            invitation.project_id,
            invitation.email,
            invitation.roles,
            invitation.inviter_user_id,
            invitation.expires_at,
            project.owner_user_id
        FROM admin_project_invitations AS invitation
        JOIN admin_projects AS project
          ON project.project_id = invitation.project_id
        JOIN admin_users AS inviter_account
          ON inviter_account.user_id = invitation.inviter_user_id
         AND inviter_account.active
        JOIN admin_user_projects AS inviter_membership
          ON inviter_membership.user_id = invitation.inviter_user_id
         AND inviter_membership.project_id = invitation.project_id
        WHERE invitation.token_hash = $1
          AND invitation.accepted_at IS NULL
          AND invitation.revoked_at IS NULL
          AND invitation.expires_at > NOW()
          AND '{MEMBERS_MANAGE_ROLE}' = ANY(inviter_membership.roles)
          AND invitation.roles <@ inviter_membership.roles
          AND (
              NOT ('{MEMBERS_MANAGE_ROLE}' = ANY(invitation.roles))
              OR project.owner_user_id = invitation.inviter_user_id
          )
        {lock_clause}
        """,
        digest,
    )


@router.get(
    "/api/projects/{project_id}/members",
    response_model=ProjectMembers,
)
async def list_project_members(
    project_id: str,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ProjectMembers:
    actor_user_id = uuid.UUID(session.user_id)
    async with request.app.state.pg_pool.acquire() as conn:
        await _manager_context(
            conn,
            project_id=project_id,
            actor_user_id=actor_user_id,
            lock=False,
        )
        member_rows = await conn.fetch(
            """
            SELECT
                membership.user_id,
                account.email,
                membership.roles,
                account.active,
                (project.owner_user_id = membership.user_id) AS is_owner,
                membership.created_at AS joined_at
            FROM admin_user_projects AS membership
            JOIN admin_users AS account
              ON account.user_id = membership.user_id
            JOIN admin_projects AS project
              ON project.project_id = membership.project_id
            WHERE membership.project_id = $1
            ORDER BY account.email, membership.user_id
            """,
            project_id,
        )
        invitation_rows = await conn.fetch(
            f"""
            SELECT
                invitation.invitation_id,
                invitation.email,
                invitation.roles,
                inviter.email AS inviter_email,
                CASE
                    WHEN NOT inviter.active THEN 'inviter_inactive'
                    WHEN inviter_membership.user_id IS NULL
                        THEN 'inviter_not_project_member'
                    WHEN NOT (
                        '{MEMBERS_MANAGE_ROLE}' = ANY(inviter_membership.roles)
                    ) THEN 'inviter_lacks_members_manage'
                    WHEN NOT (invitation.roles <@ inviter_membership.roles)
                        THEN 'roles_exceed_inviter_authority'
                    WHEN '{MEMBERS_MANAGE_ROLE}' = ANY(invitation.roles)
                     AND project.owner_user_id IS DISTINCT FROM
                         invitation.inviter_user_id
                        THEN 'members_manage_requires_owner'
                    ELSE NULL
                END AS blocked_reason,
                invitation.expires_at,
                invitation.created_at
            FROM admin_project_invitations AS invitation
            JOIN admin_projects AS project
              ON project.project_id = invitation.project_id
            JOIN admin_users AS inviter
              ON inviter.user_id = invitation.inviter_user_id
            LEFT JOIN admin_user_projects AS inviter_membership
              ON inviter_membership.user_id = invitation.inviter_user_id
             AND inviter_membership.project_id = invitation.project_id
            WHERE invitation.project_id = $1
              AND invitation.accepted_at IS NULL
              AND invitation.revoked_at IS NULL
              AND invitation.expires_at > NOW()
            ORDER BY invitation.created_at DESC, invitation.invitation_id DESC
            """,
            project_id,
        )
    return ProjectMembers(
        members=[_member(row) for row in member_rows],
        pending_invitations=[_invitation(row) for row in invitation_rows],
    )


@router.post(
    "/api/projects/{project_id}/invitations",
    response_model=ProjectInvitationReveal,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_invitation(
    project_id: str,
    body: InvitationCreateRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ProjectInvitationReveal:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    actor_user_id = uuid.UUID(session.user_id)
    email = body.email.strip().lower()
    roles = list(body.roles)
    raw_token = new_token()

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            actor = await _manager_context(
                conn,
                project_id=project_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            _assert_role_grant(
                actor_roles=list(actor["roles"]),
                actor_is_owner=bool(actor["is_owner"]),
                requested_roles=roles,
            )
            existing_member = await conn.fetchval(
                """
                SELECT membership.user_id
                FROM admin_users AS account
                JOIN admin_user_projects AS membership
                  ON membership.user_id = account.user_id
                 AND membership.project_id = $1
                WHERE account.email = $2
                FOR KEY SHARE OF account, membership
                """,
                project_id,
                email,
            )
            if existing_member is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This account is already a project member",
                )

            expired = await conn.fetchrow(
                """
                SELECT invitation_id, roles
                FROM admin_project_invitations
                WHERE project_id = $1
                  AND email = $2
                  AND accepted_at IS NULL
                  AND revoked_at IS NULL
                  AND expires_at <= NOW()
                FOR UPDATE
                """,
                project_id,
                email,
            )
            if expired is not None:
                await conn.execute(
                    """
                    UPDATE admin_project_invitations
                    SET revoked_at = NOW()
                    WHERE invitation_id = $1
                    """,
                    expired["invitation_id"],
                )
                await _record_membership_audit(
                    conn,
                    project_id=project_id,
                    action="invitation_revoke",
                    actor_user_id=actor_user_id,
                    subject_email=email,
                    invitation_id=expired["invitation_id"],
                    previous_roles=list(expired["roles"]),
                )

            row = await conn.fetchrow(
                """
                INSERT INTO admin_project_invitations (
                    invitation_id,
                    token_hash,
                    project_id,
                    email,
                    roles,
                    inviter_user_id,
                    expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW() + INTERVAL '7 days')
                ON CONFLICT DO NOTHING
                RETURNING invitation_id, email, roles, expires_at, created_at
                """,
                uuid.uuid4(),
                token_hash(raw_token),
                project_id,
                email,
                roles,
                actor_user_id,
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A pending invitation already exists for this email",
                )
            await _record_membership_audit(
                conn,
                project_id=project_id,
                action="invitation_create",
                actor_user_id=actor_user_id,
                subject_email=email,
                invitation_id=row["invitation_id"],
                new_roles=roles,
            )

    origin = request.headers["origin"].rstrip("/")
    return ProjectInvitationReveal(
        **_invitation(
            {
                **dict(row),
                "inviter_email": session.email,
                "blocked_reason": None,
            }
        ).model_dump(),
        invitation_url=f"{origin}/invitations/{raw_token}",
    )


@router.delete(
    "/api/projects/{project_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_project_invitation(
    project_id: str,
    invitation_id: uuid.UUID,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> Response:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    actor_user_id = uuid.UUID(session.user_id)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            actor = await _manager_context(
                conn,
                project_id=project_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            invitation = await conn.fetchrow(
                """
                SELECT invitation_id, email, roles
                FROM admin_project_invitations
                WHERE invitation_id = $1
                  AND project_id = $2
                  AND accepted_at IS NULL
                  AND revoked_at IS NULL
                FOR UPDATE
                """,
                invitation_id,
                project_id,
            )
            if invitation is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Pending invitation not found",
                )
            _assert_role_grant(
                actor_roles=list(actor["roles"]),
                actor_is_owner=bool(actor["is_owner"]),
                requested_roles=list(invitation["roles"]),
                current_roles=list(invitation["roles"]),
            )
            await conn.execute(
                """
                UPDATE admin_project_invitations
                SET revoked_at = NOW()
                WHERE invitation_id = $1
                """,
                invitation_id,
            )
            await _record_membership_audit(
                conn,
                project_id=project_id,
                action="invitation_revoke",
                actor_user_id=actor_user_id,
                subject_email=str(invitation["email"]),
                invitation_id=invitation_id,
                previous_roles=list(invitation["roles"]),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/api/projects/{project_id}/members/{member_user_id}/roles",
    response_model=ProjectMember,
)
async def replace_project_member_roles(
    project_id: str,
    member_user_id: uuid.UUID,
    body: MemberRolesReplaceRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ProjectMember:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    actor_user_id = uuid.UUID(session.user_id)
    roles = list(body.roles)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            actor = await _manager_context(
                conn,
                project_id=project_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            target = await conn.fetchrow(
                """
                SELECT
                    membership.user_id,
                    account.email,
                    membership.roles,
                    account.active,
                    (project.owner_user_id = membership.user_id) AS is_owner,
                    membership.created_at AS joined_at
                FROM admin_user_projects AS membership
                JOIN admin_users AS account
                  ON account.user_id = membership.user_id
                JOIN admin_projects AS project
                  ON project.project_id = membership.project_id
                WHERE membership.project_id = $1
                  AND membership.user_id = $2
                FOR UPDATE OF membership, account
                """,
                project_id,
                member_user_id,
            )
            if target is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project member not found",
                )
            if target["is_owner"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Transfer ownership before changing the owner's roles",
                )
            current_roles = list(target["roles"])
            if current_roles == roles:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Member already has this exact role set",
                )
            _assert_role_grant(
                actor_roles=list(actor["roles"]),
                actor_is_owner=bool(actor["is_owner"]),
                requested_roles=roles,
                current_roles=current_roles,
            )
            await conn.execute(
                """
                UPDATE admin_user_projects
                SET roles = $3
                WHERE project_id = $1
                  AND user_id = $2
                """,
                project_id,
                member_user_id,
                roles,
            )
            await _record_membership_audit(
                conn,
                project_id=project_id,
                action="roles_replace",
                actor_user_id=actor_user_id,
                subject_user_id=member_user_id,
                subject_email=str(target["email"]),
                previous_roles=current_roles,
                new_roles=roles,
            )
    return _member({**dict(target), "roles": roles})


@router.delete(
    "/api/projects/{project_id}/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_project_member(
    project_id: str,
    member_user_id: uuid.UUID,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> Response:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    actor_user_id = uuid.UUID(session.user_id)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            actor = await _manager_context(
                conn,
                project_id=project_id,
                actor_user_id=actor_user_id,
                lock=True,
            )
            target = await conn.fetchrow(
                """
                SELECT
                    membership.user_id,
                    account.email,
                    membership.roles,
                    (project.owner_user_id = membership.user_id) AS is_owner
                FROM admin_user_projects AS membership
                JOIN admin_users AS account
                  ON account.user_id = membership.user_id
                JOIN admin_projects AS project
                  ON project.project_id = membership.project_id
                WHERE membership.project_id = $1
                  AND membership.user_id = $2
                FOR UPDATE OF membership, account
                """,
                project_id,
                member_user_id,
            )
            if target is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project member not found",
                )
            if target["is_owner"]:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Transfer ownership before removing the owner",
                )
            _assert_role_grant(
                actor_roles=list(actor["roles"]),
                actor_is_owner=bool(actor["is_owner"]),
                requested_roles=list(target["roles"]),
                current_roles=list(target["roles"]),
            )
            await _record_membership_audit(
                conn,
                project_id=project_id,
                action="member_remove",
                actor_user_id=actor_user_id,
                subject_user_id=member_user_id,
                subject_email=str(target["email"]),
                previous_roles=list(target["roles"]),
            )
            await conn.execute(
                """
                DELETE FROM admin_user_projects
                WHERE project_id = $1
                  AND user_id = $2
                """,
                project_id,
                member_user_id,
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/api/projects/{project_id}/members/audit",
    response_model=MembershipAuditPage,
)
async def list_membership_audit(
    project_id: str,
    request: Request,
    page: Annotated[AuditPageQuery, Query()],
    session: AdminSession = Depends(require_session),
) -> MembershipAuditPage:
    async with request.app.state.pg_pool.acquire() as conn:
        await _manager_context(
            conn,
            project_id=project_id,
            actor_user_id=uuid.UUID(session.user_id),
            lock=False,
        )
        rows = await conn.fetch(
            """
            SELECT
                audit.audit_id,
                audit.project_id,
                audit.action,
                audit.actor_user_id,
                actor.email AS actor_email,
                audit.subject_user_id,
                audit.subject_email,
                audit.invitation_id,
                audit.previous_roles,
                audit.new_roles,
                audit.created_at
            FROM admin_project_membership_audit AS audit
            JOIN admin_users AS actor
              ON actor.user_id = audit.actor_user_id
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
    entries = [MembershipAuditEntry(**dict(row)) for row in page_rows]
    next_cursor = (
        AuditCursor(
            created_at=page_rows[-1]["created_at"],
            audit_id=page_rows[-1]["audit_id"],
        )
        if len(rows) > page.limit
        else None
    )
    return MembershipAuditPage(entries=entries, next_cursor=next_cursor)


@router.get(
    "/api/invitations/{raw_token}",
    response_model=InvitationInspection,
)
async def inspect_invitation(
    raw_token: str,
    request: Request,
) -> InvitationInspection:
    if TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise _unavailable_invitation()
    digest = token_hash(raw_token)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await _rate_limit_invitation(
                conn,
                request=request,
                digest=digest,
            )
            invitation = await _valid_invitation(conn, digest=digest, lock=False)
    if invitation is None:
        raise _unavailable_invitation()
    return InvitationInspection(
        project_id=str(invitation["project_id"]),
        email=str(invitation["email"]),
        roles=list(invitation["roles"]),
        expires_at=invitation["expires_at"],
    )


@router.post(
    "/api/invitations/{raw_token}/accept",
    response_model=UserIdentity,
)
async def accept_invitation(
    raw_token: str,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> UserIdentity:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    if TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise _unavailable_invitation()
    digest = token_hash(raw_token)
    user_id = uuid.UUID(session.user_id)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await _rate_limit_invitation(conn, request=request, digest=digest)
            invitation = await _valid_invitation(conn, digest=digest, lock=True)
            account = await conn.fetchrow(
                """
                SELECT user_id, email, active
                FROM admin_users
                WHERE user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            if (
                invitation is None
                or account is None
                or not account["active"]
                or str(account["email"]) != str(invitation["email"])
            ):
                raise _unavailable_invitation()
            inserted = await conn.fetchval(
                """
                INSERT INTO admin_user_projects (user_id, project_id, roles)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, project_id) DO NOTHING
                RETURNING user_id
                """,
                user_id,
                invitation["project_id"],
                list(invitation["roles"]),
            )
            if inserted is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This account is already a project member",
                )
            await conn.execute(
                """
                UPDATE admin_project_invitations
                SET accepted_at = NOW(),
                    accepted_by_user_id = $2
                WHERE invitation_id = $1
                """,
                invitation["invitation_id"],
                user_id,
            )
            await _record_membership_audit(
                conn,
                project_id=str(invitation["project_id"]),
                action="invitation_accept",
                actor_user_id=user_id,
                subject_user_id=user_id,
                subject_email=str(invitation["email"]),
                invitation_id=invitation["invitation_id"],
                new_roles=list(invitation["roles"]),
            )

    projects = dict(session.projects)
    projects[str(invitation["project_id"])] = frozenset(invitation["roles"])
    return UserIdentity(
        user_id=session.user_id,
        email=session.email,
        projects=[
            ProjectAccess(project_id=project_id, roles=sorted(roles))
            for project_id, roles in sorted(projects.items())
        ],
    )


@router.post(
    "/api/invitations/{raw_token}/register",
    response_model=UserIdentity,
    status_code=status.HTTP_201_CREATED,
)
async def register_with_invitation(
    raw_token: str,
    body: InvitationRegistrationRequest,
    request: Request,
    response: Response,
) -> UserIdentity:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    if TOKEN_PATTERN.fullmatch(raw_token) is None:
        raise _unavailable_invitation()
    digest = token_hash(raw_token)

    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            source = await _rate_limit_invitation(
                conn,
                request=request,
                digest=digest,
            )
            inspection = await _valid_invitation(conn, digest=digest, lock=False)
    if inspection is None:
        raise _unavailable_invitation()

    password_hash = await asyncio.to_thread(hash_password, body.password)
    now = datetime.now(timezone.utc)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1)",
                ACCOUNT_REGISTRATION_LOCK_ID,
            )
            invitation = await _valid_invitation(conn, digest=digest, lock=True)
            if invitation is None:
                raise _unavailable_invitation()
            existing_account = await conn.fetchval(
                "SELECT user_id FROM admin_users WHERE email = $1 FOR UPDATE",
                invitation["email"],
            )
            if existing_account is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="An account already exists for this invitation email",
                )
            account_count = int(await conn.fetchval("SELECT count(*) FROM admin_users"))
            if account_count >= settings.max_accounts:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This deployment has reached its account limit",
                )

            user_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO admin_users (user_id, email, password_hash)
                VALUES ($1, $2, $3)
                """,
                user_id,
                invitation["email"],
                password_hash,
            )
            await conn.execute(
                """
                INSERT INTO admin_user_projects (user_id, project_id, roles)
                VALUES ($1, $2, $3)
                """,
                user_id,
                invitation["project_id"],
                list(invitation["roles"]),
            )
            await conn.execute(
                """
                UPDATE admin_project_invitations
                SET accepted_at = NOW(),
                    accepted_by_user_id = $2
                WHERE invitation_id = $1
                """,
                invitation["invitation_id"],
                user_id,
            )
            await _record_membership_audit(
                conn,
                project_id=str(invitation["project_id"]),
                action="invitation_accept",
                actor_user_id=user_id,
                subject_user_id=user_id,
                subject_email=str(invitation["email"]),
                invitation_id=invitation["invitation_id"],
                new_roles=list(invitation["roles"]),
            )
            session_token, csrf_token = await _start_session(
                conn,
                user_id,
                settings,
                now,
            )

    set_device_cookie(response, source, settings)
    set_session_cookies(response, session_token, csrf_token, settings)
    return UserIdentity(
        user_id=str(user_id),
        email=str(invitation["email"]),
        projects=[
            ProjectAccess(
                project_id=str(invitation["project_id"]),
                roles=list(invitation["roles"]),
            )
        ],
    )
