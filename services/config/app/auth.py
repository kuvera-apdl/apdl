"""Database-backed API-key authentication and project authorization."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

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
_CAPABILITY_PATTERN = re.compile(r"^apdlcap_[A-Za-z0-9_-]{43}$")
_CAPABILITY_HEADER = "x-apdl-internal-capability"
_CAPABILITY_REQUEST_SCHEMA = "apdl_service_request@1"
_CAPABILITY_ROLE_SETS = frozenset(
    {
        frozenset({"config:write"}),
        frozenset({"config:evaluate"}),
        frozenset({"agents:read"}),
        frozenset({"query:read"}),
    }
)

logger = logging.getLogger(__name__)


def _canonical_request_sha256(
    *,
    method: str,
    path: str,
    json_body: Any,
    idempotency_key: str | None,
) -> str:
    """Hash the strict request envelope shared with the capability issuer."""
    if re.fullmatch(r"[A-Z]+", method) is None:
        raise ValueError("Capability request method is not canonical")
    if (
        not path.startswith("/")
        or "?" in path
        or "#" in path
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in path)
    ):
        raise ValueError("Capability request path is not canonical")
    idempotency_key_sha256 = (
        hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        if idempotency_key is not None
        else None
    )
    canonical = json.dumps(
        {
            "body": json_body,
            "idempotency_key_sha256": idempotency_key_sha256,
            "method": method,
            "path": path,
            "schema_version": _CAPABILITY_REQUEST_SCHEMA,
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _reject_duplicate_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> Any:
    raise ValueError(f"Non-finite JSON number: {value}")


async def _request_sha256(request: Request) -> str:
    raw_body = await request.body()
    json_body = (
        json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_object,
            parse_constant=_reject_nonfinite_json,
        )
        if raw_body
        else None
    )
    raw_path = request.scope.get("raw_path")
    path = (
        raw_path.decode("ascii")
        if isinstance(raw_path, bytes)
        else request.url.path
    )
    return _canonical_request_sha256(
        method=request.method,
        path=path,
        json_body=json_body,
        idempotency_key=request.headers.get("idempotency-key"),
    )


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
    auth_kind: Literal["api_key", "internal_capability"] = "api_key"
    capability_run_id: str | None = None
    capability_execution_kind: str | None = None


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

    async def authenticate_capability(
        self,
        token: str,
        request: Request | None = None,
    ) -> Principal | None:
        """Verify one Config-audience capability and its live execution lease."""
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
                  AND 'config' = ANY(capability.audiences)
                  AND capability.issued_at <= now()
                  AND capability.expires_at > now()
                  AND capability.consumed_at IS NULL
                """,
                provided_hash,
            )
            if row is None or not row["execution_active"]:
                return None
            if not secrets.compare_digest(provided_hash, str(row["token_hash"])):
                return None
            roles = frozenset(str(role) for role in row["roles"])
            if roles not in _CAPABILITY_ROLE_SETS:
                return None
            expected_request_sha256 = row["request_sha256"]
            if roles == frozenset({"config:write"}):
                if (
                    str(row["execution_kind"]) != "approval_effect"
                    or expected_request_sha256 is None
                    or request is None
                ):
                    return None
                try:
                    provided_request_sha256 = await _request_sha256(request)
                except (UnicodeDecodeError, ValueError, TypeError):
                    return None
                if not secrets.compare_digest(
                    provided_request_sha256,
                    str(expected_request_sha256),
                ):
                    return None
                consumed = await conn.fetchval(
                    """
                    SELECT public.apdl_consume_agent_service_capability(
                        $1::uuid,
                        $2,
                        'config',
                        'config:write',
                        $3
                    )
                    """,
                    row["capability_id"],
                    provided_hash,
                    provided_request_sha256,
                )
                if consumed is not True:
                    return None
            elif expected_request_sha256 is not None or row["consumed_at"] is not None:
                return None
        return Principal(
            credential_id=f"agentcap-{row['capability_id']}",
            project_id=str(row["project_id"]),
            roles=roles,
            auth_kind="internal_capability",
            capability_run_id=str(row["run_id"]),
            capability_execution_kind=str(row["execution_kind"]),
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
    """Authenticate exactly one external key or internal capability."""
    if "api_key" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credentials are not accepted in query parameters",
        )
    api_key = request.headers.get("x-api-key", "")
    capability = request.headers.get(_CAPABILITY_HEADER, "")
    if api_key and capability:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multiple authentication mechanisms are not allowed",
        )
    if capability and request.url.path == "/v1/stream":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Internal capabilities are not accepted for streams",
        )
    try:
        principal = (
            await request.app.state.authenticator.authenticate_capability(
                capability,
                request,
            )
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


def authorized_project(request: Request, role: str) -> str:
    principal = require_role(request, role)
    requested_project = request.query_params.get("project_id")
    if requested_project and requested_project != principal.project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    return principal.project_id


def authorized_project_any_role(
    request: Request,
    roles: frozenset[str],
) -> str:
    """Authorize a project-scoped route with one of an exact role set."""
    principal: Principal = request.state.principal
    if not principal.roles & roles:
        rendered = ", ".join(sorted(roles))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Credential requires one role: {rendered}",
        )
    requested_project = request.query_params.get("project_id")
    if requested_project and requested_project != principal.project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Credential is not authorized for this project",
        )
    return principal.project_id
