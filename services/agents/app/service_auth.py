"""Short-lived, execution-bound authority for internal service calls."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import asyncpg


CapabilityAudience = Literal["config", "query", "codegen"]
CapabilityRole = Literal[
    "config:write",
    "config:evaluate",
    "query:read",
    "agents:read",
    "agents:manage",
]
CapabilityExecutionKind = Literal[
    "agent_run",
    "custom_agent_test",
    "approval_effect",
]

CAPABILITY_HEADER = "X-APDL-Internal-Capability"
CAPABILITY_TTL_SECONDS = 60
CAPABILITY_REQUEST_SCHEMA = "apdl_service_request@1"
logger = logging.getLogger(__name__)
_TOKEN_PATTERN = re.compile(r"^apdlcap_[A-Za-z0-9_-]{43}$")
_PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,64}$")
_IDENTITY_PATTERN = re.compile(r"^\S{1,128}$")
_OWNER_PATTERN = re.compile(r"^\S{1,512}$")
_AUDIENCE_ORDER: tuple[CapabilityAudience, ...] = ("config", "query", "codegen")
_ROLE_ORDER: tuple[CapabilityRole, ...] = (
    "config:write",
    "config:evaluate",
    "query:read",
    "agents:read",
    "agents:manage",
)
_AUTHORITY_SHAPES = frozenset(
    {
        (("config",), ("config:write",)),
        (("config",), ("config:evaluate",)),
        (("config",), ("agents:read",)),
        (("config",), ("query:read",)),
        (("query",), ("query:read",)),
        (("config", "query"), ("query:read",)),
        (("codegen",), ("agents:read",)),
        (("codegen",), ("agents:manage",)),
    }
)
_MUTATION_ROLES = frozenset({"config:write", "agents:manage"})
_UNSET_REQUEST_BODY = object()


class ServiceCapabilityUnavailableError(RuntimeError):
    """The durable execution no longer owns authority to call another service."""


@dataclass(frozen=True)
class ServiceCapabilityContext:
    """Exact durable execution identity used to mint one JIT capability."""

    pool: asyncpg.Pool
    project_id: str
    execution_kind: CapabilityExecutionKind
    execution_id: str
    run_id: str
    execution_owner_id: str

    def __post_init__(self) -> None:
        if _PROJECT_PATTERN.fullmatch(self.project_id) is None:
            raise ValueError("project_id must match ^[A-Za-z0-9]{1,64}$")
        if self.execution_kind not in {
            "agent_run",
            "custom_agent_test",
            "approval_effect",
        }:
            raise ValueError("Unknown capability execution_kind")
        if _IDENTITY_PATTERN.fullmatch(self.execution_id) is None:
            raise ValueError("execution_id must be 1 to 128 non-whitespace characters")
        if _IDENTITY_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("run_id must be 1 to 128 non-whitespace characters")
        if _OWNER_PATTERN.fullmatch(self.execution_owner_id) is None:
            raise ValueError(
                "execution_owner_id must be 1 to 512 non-whitespace characters"
            )


def _canonical_values[T: str](
    selected: tuple[T, ...],
    order: tuple[T, ...],
    *,
    label: str,
) -> tuple[T, ...]:
    expected = tuple(value for value in order if value in selected)
    if not selected or selected != expected:
        raise ValueError(f"{label} must be unique and use canonical order")
    return selected


def canonical_request_sha256(
    *,
    method: str,
    path: str,
    json_body: Any,
    idempotency_key: str | None,
) -> str:
    """Hash the exact canonical HTTP mutation request authorized by a token."""
    if re.fullmatch(r"[A-Z]+", method) is None:
        raise ValueError("Capability request method must be canonical uppercase ASCII")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "?" in path
        or "#" in path
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in path)
    ):
        raise ValueError("Capability request path must be one exact ASCII path")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise ValueError("Capability Idempotency-Key must be a string when present")
    idempotency_key_sha256 = (
        hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        if idempotency_key is not None
        else None
    )
    try:
        canonical = json.dumps(
            {
                "body": json_body,
                "idempotency_key_sha256": idempotency_key_sha256,
                "method": method,
                "path": path,
                "schema_version": CAPABILITY_REQUEST_SCHEMA,
            },
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Capability request body must be canonical JSON data") from exc
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


async def _execution_is_active(
    conn: Any,
    context: ServiceCapabilityContext,
) -> bool:
    if context.execution_kind == "agent_run":
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_runs
                    WHERE run_id = $1
                      AND project_id = $2
                      AND execution_lane_project_id = project_id
                      AND lease_owner_id = $3
                      AND lease_expires_at > now()
                      AND (
                        status IN ('started', 'running')
                        OR (phase = 'resuming' AND status IN ('approved', 'rejected'))
                      )
                )
                """,
                context.run_id,
                context.project_id,
                context.execution_owner_id,
            )
        )
    if context.execution_kind == "custom_agent_test":
        return bool(
            await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM custom_agent_test_runs
                    WHERE test_run_id = $1
                      AND project_id = $2
                      AND status = 'running'
                      AND lease_expires_at > now()
                )
                """,
                context.execution_id,
                context.project_id,
            )
        )
    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM agent_approval_effects AS effect
                JOIN agent_runs AS run
                  ON run.run_id = effect.run_id
                 AND run.project_id = effect.project_id
                WHERE effect.effect_id = $1::uuid
                  AND effect.run_id = $2
                  AND effect.project_id = $3
                  AND effect.status = 'processing'
                  AND effect.lease_owner_id = $4
                  AND effect.lease_expires_at > now()
                  AND run.execution_lane_project_id = run.project_id
                  AND run.status IN ('approval_queued', 'cancelling')
            )
            """,
            context.execution_id,
            context.run_id,
            context.project_id,
            context.execution_owner_id,
        )
    )


@asynccontextmanager
async def service_headers(
    context: ServiceCapabilityContext,
    *,
    audiences: tuple[CapabilityAudience, ...],
    roles: tuple[CapabilityRole, ...],
    request_method: str | None = None,
    request_path: str | None = None,
    request_json: Any = _UNSET_REQUEST_BODY,
    idempotency_key: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    """Yield one JIT capability and revoke it when the HTTP call completes."""
    canonical_audiences = _canonical_values(
        audiences,
        _AUDIENCE_ORDER,
        label="audiences",
    )
    canonical_roles = _canonical_values(roles, _ROLE_ORDER, label="roles")
    if (canonical_audiences, canonical_roles) not in _AUTHORITY_SHAPES:
        raise ValueError("audiences and roles are not an allowed authority shape")
    mutation = bool(set(canonical_roles) & _MUTATION_ROLES)
    if context.execution_kind != "approval_effect" and mutation:
        raise ValueError(
            "Only a leased approval effect may receive mutation authority"
        )
    if mutation:
        if (
            request_method is None
            or request_path is None
            or request_json is _UNSET_REQUEST_BODY
        ):
            raise ValueError(
                "Mutation authority requires one exact canonical request binding"
            )
        request_sha256 = canonical_request_sha256(
            method=request_method,
            path=request_path,
            json_body=request_json,
            idempotency_key=idempotency_key,
        )
    else:
        if (
            request_method is not None
            or request_path is not None
            or request_json is not _UNSET_REQUEST_BODY
            or idempotency_key is not None
        ):
            raise ValueError("Read authority must not carry a mutation request binding")
        request_sha256 = None
    raw_token = f"apdlcap_{secrets.token_urlsafe(32)}"
    if _TOKEN_PATTERN.fullmatch(raw_token) is None:  # pragma: no cover - stdlib guard
        raise RuntimeError("Generated capability token is not canonical")
    token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()

    async with context.pool.acquire() as conn:
        async with conn.transaction():
            if not await _execution_is_active(conn, context):
                raise ServiceCapabilityUnavailableError(
                    f"{context.execution_kind} {context.execution_id} no longer owns "
                    "internal service authority"
                )
            await conn.execute(
                "DELETE FROM agent_service_capabilities WHERE expires_at <= now()"
            )
            await conn.execute(
                """
                INSERT INTO agent_service_capabilities (
                    token_hash, project_id, execution_kind, execution_id,
                    run_id, execution_owner_id, audiences, roles,
                    request_sha256, expires_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::text[], $8::text[],
                    $9, now() + ($10 * interval '1 second')
                )
                """,
                token_hash,
                context.project_id,
                context.execution_kind,
                context.execution_id,
                context.run_id,
                context.execution_owner_id,
                list(canonical_audiences),
                list(canonical_roles),
                request_sha256,
                CAPABILITY_TTL_SECONDS,
            )
    try:
        yield {CAPABILITY_HEADER: raw_token}
    finally:
        try:
            async with context.pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM agent_service_capabilities WHERE token_hash = $1",
                    token_hash,
                )
        except Exception:
            logger.exception("Failed to revoke internal service capability")
