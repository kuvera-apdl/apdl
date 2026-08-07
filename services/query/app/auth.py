"""Database-backed API-key authentication and project authorization."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException, Request, status

_API_KEY_PATTERN = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)
_CAPABILITY_PATTERN = re.compile(r"^apdlcap_[A-Za-z0-9_-]{43}$")
_CAPABILITY_HEADER = "x-apdl-internal-capability"
_DUMMY_KEY_HASH = "0" * 64

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """Verified credential authority. Project and roles only come from storage."""

    credential_id: str
    project_id: str
    roles: frozenset[str]
    auth_kind: Literal["api_key", "internal_capability"] = "api_key"
    capability_run_id: str | None = None
    capability_execution_kind: str | None = None


class PostgresAuthenticator:
    """Verify API keys against the canonical PostgreSQL credential registry."""

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

    async def authenticate_capability(self, token: str) -> Principal | None:
        """Verify one Query-audience capability and its live execution lease."""
        if _CAPABILITY_PATTERN.fullmatch(token) is None:
            return None
        provided_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT capability.capability_id, capability.token_hash,
                       capability.project_id, capability.execution_kind,
                       capability.execution_id, capability.run_id,
                       capability.execution_owner_id, capability.roles,
                       capability.request_sha256, capability.consumed_at,
                       (
                         (
                           capability.execution_kind = 'agent_run'
                           AND EXISTS (
                             SELECT 1 FROM agent_runs AS run
                             WHERE run.run_id = capability.run_id
                               AND run.project_id = capability.project_id
                               AND run.execution_lane_project_id = run.project_id
                               AND run.lease_owner_id = capability.execution_owner_id
                               AND run.lease_expires_at > now()
                               AND (
                                 run.status IN ('started', 'running')
                                 OR (
                                   run.phase = 'resuming'
                                   AND run.status IN ('approved', 'rejected')
                                 )
                               )
                           )
                         ) OR (
                           capability.execution_kind = 'custom_agent_test'
                           AND EXISTS (
                             SELECT 1 FROM custom_agent_test_runs AS test
                             WHERE test.test_run_id = capability.execution_id
                               AND test.project_id = capability.project_id
                               AND test.status = 'running'
                               AND test.lease_expires_at > now()
                           )
                         ) OR (
                           capability.execution_kind = 'approval_effect'
                           AND EXISTS (
                             SELECT 1
                             FROM agent_approval_effects AS effect
                             JOIN agent_runs AS run
                               ON run.run_id = effect.run_id
                              AND run.project_id = effect.project_id
                             WHERE effect.effect_id::text = capability.execution_id
                               AND effect.run_id = capability.run_id
                               AND effect.project_id = capability.project_id
                               AND effect.status = 'processing'
                               AND effect.lease_owner_id = capability.execution_owner_id
                               AND effect.lease_expires_at > now()
                               AND run.execution_lane_project_id = run.project_id
                               AND run.status IN ('approval_queued', 'cancelling')
                           )
                         )
                       ) AS execution_active
                FROM agent_service_capabilities AS capability
                WHERE capability.token_hash = $1
                  AND 'query' = ANY(capability.audiences)
                  AND capability.issued_at <= now()
                  AND capability.expires_at > now()
                """,
                provided_hash,
            )
        if row is None or not row["execution_active"]:
            return None
        if not secrets.compare_digest(provided_hash, str(row["token_hash"])):
            return None
        roles = frozenset(str(role) for role in row["roles"])
        if roles != frozenset({"query:read"}):
            return None
        if row["request_sha256"] is not None or row["consumed_at"] is not None:
            return None
        return Principal(
            credential_id=f"agentcap-{row['capability_id']}",
            project_id=str(row["project_id"]),
            roles=roles,
            auth_kind="internal_capability",
            capability_run_id=str(row["run_id"]),
            capability_execution_kind=str(row["execution_kind"]),
        )


async def authenticate_request(request: Request) -> Principal:
    """Authenticate exactly one external key or internal capability."""
    api_key = request.headers.get("x-api-key", "")
    capability = request.headers.get(_CAPABILITY_HEADER, "")
    if api_key and capability:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multiple authentication mechanisms are not allowed",
        )
    try:
        principal = (
            await request.app.state.authenticator.authenticate_capability(capability)
            if capability
            else await request.app.state.authenticator.authenticate(api_key)
        )
    except Exception as exc:
        logger.exception("Credential lookup failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        ) from exc

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid API key or internal capability required",
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


def delegated_auth_headers(request: Request) -> dict[str, str]:
    """Forward the exact already-verified authority to Config."""
    principal: Principal = request.state.principal
    if principal.auth_kind == "internal_capability":
        token = request.headers.get(_CAPABILITY_HEADER, "")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Validated internal capability is unavailable for delegation",
            )
        return {"X-APDL-Internal-Capability": token}
    api_key = request.headers.get("x-api-key", "")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Validated API key is unavailable for delegation",
        )
    return {"X-API-Key": api_key}
