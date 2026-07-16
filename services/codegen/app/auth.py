"""Database-backed, project-scoped authentication for Codegen."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request, status

_API_KEY_PATTERN = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)
_DUMMY_KEY_HASH = "0" * 64

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Verified credential authority derived only from PostgreSQL."""

    credential_id: str
    project_id: str
    roles: frozenset[str]


class PostgresAuthenticator:
    """Verify canonical API keys against the shared credential registry."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def authenticate(self, api_key: str) -> Principal | None:
        key_match = _API_KEY_PATTERN.fullmatch(api_key)
        provided_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

        row = None
        if key_match is not None:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT credential_id, project_id, key_hash, roles,
                           active, expires_at
                    FROM auth_credentials
                    WHERE key_hash = $1
                    """,
                    provided_hash,
                )

        expected_hash = str(row["key_hash"]) if row is not None else _DUMMY_KEY_HASH
        key_valid = secrets.compare_digest(provided_hash, expected_hash)
        if row is None or key_match is None or not key_valid or not row["active"]:
            return None

        stored_project = str(row["project_id"])
        if not secrets.compare_digest(key_match.group("project_id"), stored_project):
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
            roles=frozenset(str(role) for role in row["roles"]),
        )


async def authenticate_request(request: Request) -> Principal:
    """Authenticate the canonical ``X-API-Key`` header."""
    api_key = request.headers.get("x-api-key", "")
    try:
        principal = await request.app.state.authenticator.authenticate(api_key)
    except Exception as exc:
        logger.exception("Codegen credential lookup failed")
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
    if not secrets.compare_digest(principal.project_id, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    return principal
