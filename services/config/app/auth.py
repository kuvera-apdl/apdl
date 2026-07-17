"""Database-backed API-key authentication and project authorization."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

_DUMMY_KEY_HASH = "0" * 64
_ALLOWED_ROLES = frozenset({
    "events:write",
    "config:read",
    "config:write",
    "config:evaluate",
    "query:read",
    "agents:read",
    "agents:run",
    "agents:manage",
    "agents:approve",
})
_BROWSER_ROLES = frozenset({"events:write", "config:read"})

logger = logging.getLogger(__name__)


class CredentialKind(str, Enum):
    """Canonical storage and wire kinds for APDL credentials."""

    CONFIDENTIAL = "confidential"
    BROWSER = "browser"


_KEY_PATTERNS = {
    CredentialKind.CONFIDENTIAL: re.compile(
        r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
    ),
    CredentialKind.BROWSER: re.compile(
        r"^client_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
    ),
}


@dataclass(frozen=True)
class CredentialDescriptor:
    kind: CredentialKind
    project_id: str
    key_prefix: str


def _parse_credential(api_key: str) -> CredentialDescriptor | None:
    for kind, pattern in _KEY_PATTERNS.items():
        match = pattern.fullmatch(api_key)
        if match is None:
            continue
        project_id = match.group("project_id")
        wire_prefix = "proj" if kind is CredentialKind.CONFIDENTIAL else "client"
        return CredentialDescriptor(
            kind=kind,
            project_id=project_id,
            key_prefix=f"{wire_prefix}_{project_id}_",
        )
    return None


@dataclass(frozen=True)
class Principal:
    """Verified credential authority. Project and roles only come from storage."""

    credential_id: str
    project_id: str
    roles: frozenset[str]


class AuthIdentity(BaseModel):
    """Canonical authenticated identity returned to first-party clients."""

    model_config = ConfigDict(extra="forbid")

    credential_id: str
    project_id: str
    roles: list[str]


class PostgresAuthenticator:
    """Verify API keys against the canonical PostgreSQL credential registry."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def authenticate(self, api_key: str) -> Principal | None:
        descriptor = _parse_credential(api_key)
        provided_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

        row = None
        if descriptor is not None:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT credential_id, project_id, credential_kind,
                           key_prefix, key_hash, roles, active, expires_at
                    FROM auth_credentials
                    WHERE key_hash = $1
                    """,
                    provided_hash,
                )

        expected_hash = str(row["key_hash"]) if row is not None else _DUMMY_KEY_HASH
        key_valid = secrets.compare_digest(provided_hash, expected_hash)
        if row is None or descriptor is None or not key_valid or not row["active"]:
            return None

        stored_project = str(row["project_id"])
        stored_kind = str(row["credential_kind"])
        stored_prefix = str(row["key_prefix"])
        stored_roles = tuple(str(role) for role in row["roles"])
        roles = frozenset(stored_roles)
        if not secrets.compare_digest(descriptor.project_id, stored_project):
            return None
        if not secrets.compare_digest(descriptor.kind.value, stored_kind):
            return None
        if not secrets.compare_digest(descriptor.key_prefix, stored_prefix):
            return None
        if (
            not roles
            or len(stored_roles) != len(roles)
            or not roles <= _ALLOWED_ROLES
        ):
            return None
        if descriptor.kind is CredentialKind.BROWSER and roles != _BROWSER_ROLES:
            return None

        expires_at = row["expires_at"]
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None

        return Principal(
            credential_id=str(row["credential_id"]),
            project_id=stored_project,
            roles=roles,
        )


async def credential_has_current_role(
    pool: Any,
    principal: Principal,
    role: str,
) -> bool:
    """Revalidate one established principal against the credential registry."""
    async with pool.acquire() as conn:
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM auth_credentials
                    WHERE credential_id = $1
                      AND project_id = $2
                      AND active
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > NOW())
                      AND $3::TEXT = ANY(roles)
                )
                """,
                principal.credential_id,
                principal.project_id,
                role,
            )
        )


async def authenticate_request(request: Request) -> Principal:
    """Authenticate the canonical X-API-Key header and attach its principal."""
    if "api_key" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credentials are not accepted in query parameters",
        )
    api_key = request.headers.get("x-api-key", "")
    try:
        principal = await request.app.state.authenticator.authenticate(api_key)
    except Exception as exc:
        logger.exception("Credential lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid API key required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    request.state.principal = principal
    return principal


def require_role(request: Request, role: str) -> Principal:
    principal: Principal = request.state.principal
    if role not in principal.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Credential requires role: {role}",
        )
    return principal


def require_project(request: Request, project_id: str, role: str) -> Principal:
    principal = require_role(request, role)
    if principal.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    return principal


def authorized_project(request: Request, role: str) -> str:
    principal = require_role(request, role)
    requested_project = request.query_params.get("project_id")
    if requested_project and requested_project != principal.project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    return principal.project_id
