"""Reveal-once, human-managed project credentials."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from app.auth import AdminSession, require_csrf, require_session
from app.models import (
    CredentialActionRequest,
    CredentialAuditEntry,
    CredentialCreateRequest,
    ManagedCredential,
    ManagedCredentialReveal,
    PROJECT_ID_PATTERN,
)
from app.security import require_allowed_origin

router = APIRouter(
    prefix="/api/projects/{project_id}/credentials",
    tags=["managed credentials"],
)

ProjectId = Annotated[str, Path(pattern=PROJECT_ID_PATTERN)]
CredentialId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


async def _current_membership_roles(
    conn,
    session: AdminSession,
    project_id: str,
) -> frozenset[str]:
    row = await conn.fetchrow(
        """
        SELECT membership.roles
        FROM admin_user_projects AS membership
        JOIN admin_users AS account
          ON account.user_id = membership.user_id
        WHERE membership.user_id = $1
          AND membership.project_id = $2
          AND account.active
        FOR UPDATE OF membership, account
        """,
        uuid.UUID(session.user_id),
        project_id,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    roles = frozenset(str(role) for role in row["roles"])
    if "credentials:manage" not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role",
        )
    return roles


def _require_credential_roles(
    membership_roles: frozenset[str],
    requested_roles: list[str],
) -> None:
    missing = sorted(set(requested_roles) - membership_roles)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential roles exceed current membership",
        )


def _managed_credential(row) -> ManagedCredential:
    return ManagedCredential(
        credential_id=str(row["credential_id"]),
        project_id=str(row["project_id"]),
        credential_kind=str(row["credential_kind"]),
        key_prefix=str(row["key_prefix"]),
        roles=[str(role) for role in row["roles"]],
        active=bool(row["active"]),
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        rotated_from_credential_id=(
            str(row["rotated_from_credential_id"])
            if row["rotated_from_credential_id"] is not None
            else None
        ),
    )


async def _fetch_managed_credential(
    conn,
    project_id: str,
    credential_id: str,
    *,
    lock: bool,
):
    lock_clause = "FOR UPDATE OF credential" if lock else ""
    return await conn.fetchrow(
        f"""
        SELECT credential.credential_id, credential.project_id,
               credential.credential_kind, credential.key_prefix,
               credential.roles, credential.active, credential.revoked_at,
               managed.created_at, managed.rotated_from_credential_id
        FROM admin_managed_credentials AS managed
        JOIN auth_credentials AS credential
          ON credential.credential_id = managed.credential_id
         AND credential.project_id = managed.project_id
        WHERE managed.project_id = $1
          AND managed.credential_id = $2
        {lock_clause}
        """,
        project_id,
        credential_id,
    )


async def _record_audit(
    conn,
    *,
    session: AdminSession,
    project_id: str,
    credential_id: str,
    action: str,
    credential_kind: str,
    roles: list[str],
    successor_credential_id: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO admin_credential_audit (
            audit_id, project_id, credential_id, action,
            actor_user_id, actor_email, credential_kind, roles,
            successor_credential_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        uuid.uuid4(),
        project_id,
        credential_id,
        action,
        uuid.UUID(session.user_id),
        session.email,
        credential_kind,
        roles,
        successor_credential_id,
    )


async def _insert_managed_credential(
    conn,
    *,
    session: AdminSession,
    project_id: str,
    credential_kind: str,
    roles: list[str],
    rotated_from_credential_id: str | None = None,
) -> tuple[ManagedCredential, str]:
    credential_id = f"managed-{uuid.uuid4().hex}"
    key_prefix = (
        f"client_{project_id}_"
        if credential_kind == "browser"
        else f"proj_{project_id}_"
    )
    api_key = f"{key_prefix}{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    actor_user_id = uuid.UUID(session.user_id)

    await conn.execute(
        """
        INSERT INTO auth_credentials (
            credential_id, project_id, credential_kind, key_prefix,
            key_hash, roles
        ) VALUES ($1, $2, $3, $4, $5, $6)
        """,
        credential_id,
        project_id,
        credential_kind,
        key_prefix,
        key_hash,
        roles,
    )
    created_at = await conn.fetchval(
        """
        INSERT INTO admin_managed_credentials (
            credential_id, project_id, created_by_user_id,
            created_by_email, rotated_from_credential_id
        ) VALUES ($1, $2, $3, $4, $5)
        RETURNING created_at
        """,
        credential_id,
        project_id,
        actor_user_id,
        session.email,
        rotated_from_credential_id,
    )
    credential = ManagedCredential(
        credential_id=credential_id,
        project_id=project_id,
        credential_kind=credential_kind,
        key_prefix=key_prefix,
        roles=roles,
        active=True,
        created_at=created_at,
        revoked_at=None,
        rotated_from_credential_id=rotated_from_credential_id,
    )
    return credential, api_key


@router.get("", response_model=list[ManagedCredential])
async def list_credentials(
    project_id: ProjectId,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> list[ManagedCredential]:
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await _current_membership_roles(conn, session, project_id)
            rows = await conn.fetch(
                """
                SELECT credential.credential_id, credential.project_id,
                       credential.credential_kind, credential.key_prefix,
                       credential.roles, credential.active,
                       credential.revoked_at, managed.created_at,
                       managed.rotated_from_credential_id
                FROM admin_managed_credentials AS managed
                JOIN auth_credentials AS credential
                  ON credential.credential_id = managed.credential_id
                 AND credential.project_id = managed.project_id
                WHERE managed.project_id = $1
                ORDER BY managed.created_at DESC, managed.credential_id
                """,
                project_id,
            )
    return [_managed_credential(row) for row in rows]


@router.post(
    "",
    response_model=ManagedCredentialReveal,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential(
    project_id: ProjectId,
    body: CredentialCreateRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ManagedCredentialReveal:
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            membership_roles = await _current_membership_roles(
                conn, session, project_id
            )
            _require_credential_roles(membership_roles, body.roles)
            credential, api_key = await _insert_managed_credential(
                conn,
                session=session,
                project_id=project_id,
                credential_kind=body.credential_kind,
                roles=list(body.roles),
            )
            await _record_audit(
                conn,
                session=session,
                project_id=project_id,
                credential_id=credential.credential_id,
                action="create",
                credential_kind=credential.credential_kind,
                roles=list(credential.roles),
            )
    return ManagedCredentialReveal(**credential.model_dump(), api_key=api_key)


@router.post(
    "/{credential_id}/rotate",
    response_model=ManagedCredentialReveal,
    status_code=status.HTTP_201_CREATED,
)
async def rotate_credential(
    project_id: ProjectId,
    credential_id: CredentialId,
    body: CredentialActionRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ManagedCredentialReveal:
    del body
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            membership_roles = await _current_membership_roles(
                conn, session, project_id
            )
            row = await _fetch_managed_credential(
                conn, project_id, credential_id, lock=True
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Credential not found",
                )
            predecessor = _managed_credential(row)
            if not predecessor.active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Revoked credentials cannot be rotated",
                )
            already_rotated = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM admin_managed_credentials
                    WHERE rotated_from_credential_id = $1
                )
                """,
                credential_id,
            )
            if already_rotated:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Credential already has a successor",
                )
            _require_credential_roles(membership_roles, list(predecessor.roles))
            successor, api_key = await _insert_managed_credential(
                conn,
                session=session,
                project_id=project_id,
                credential_kind=predecessor.credential_kind,
                roles=list(predecessor.roles),
                rotated_from_credential_id=credential_id,
            )
            await _record_audit(
                conn,
                session=session,
                project_id=project_id,
                credential_id=credential_id,
                action="rotate",
                credential_kind=predecessor.credential_kind,
                roles=list(predecessor.roles),
                successor_credential_id=successor.credential_id,
            )
    return ManagedCredentialReveal(**successor.model_dump(), api_key=api_key)


@router.post("/{credential_id}/revoke", response_model=ManagedCredential)
async def revoke_credential(
    project_id: ProjectId,
    credential_id: CredentialId,
    body: CredentialActionRequest,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> ManagedCredential:
    del body
    settings = request.app.state.settings
    require_allowed_origin(request, settings)
    require_csrf(request, session)
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await _current_membership_roles(conn, session, project_id)
            row = await _fetch_managed_credential(
                conn, project_id, credential_id, lock=True
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Credential not found",
                )
            credential = _managed_credential(row)
            if not credential.active:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Credential is already revoked",
                )
            revoked_at = await conn.fetchval(
                """
                UPDATE auth_credentials
                SET active = FALSE, revoked_at = NOW()
                WHERE credential_id = $1
                  AND project_id = $2
                  AND active
                RETURNING revoked_at
                """,
                credential_id,
                project_id,
            )
            if revoked_at is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Credential is already revoked",
                )
            await _record_audit(
                conn,
                session=session,
                project_id=project_id,
                credential_id=credential_id,
                action="revoke",
                credential_kind=credential.credential_kind,
                roles=list(credential.roles),
            )
    return credential.model_copy(
        update={"active": False, "revoked_at": revoked_at}
    )


@router.get(
    "/{credential_id}/audit",
    response_model=list[CredentialAuditEntry],
)
async def credential_audit(
    project_id: ProjectId,
    credential_id: CredentialId,
    request: Request,
    session: AdminSession = Depends(require_session),
) -> list[CredentialAuditEntry]:
    async with request.app.state.pg_pool.acquire() as conn:
        async with conn.transaction():
            await _current_membership_roles(conn, session, project_id)
            credential = await _fetch_managed_credential(
                conn, project_id, credential_id, lock=False
            )
            if credential is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Credential not found",
                )
            rows = await conn.fetch(
                """
                SELECT audit_id, project_id, credential_id, action,
                       actor_user_id, actor_email, credential_kind, roles,
                       successor_credential_id, created_at
                FROM admin_credential_audit
                WHERE project_id = $1
                  AND (
                      credential_id = $2
                      OR successor_credential_id = $2
                  )
                ORDER BY created_at DESC, audit_id DESC
                """,
                project_id,
                credential_id,
            )
    return [CredentialAuditEntry.model_validate(dict(row)) for row in rows]
