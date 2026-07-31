"""Tenant-scoped executable capability checks for Codegen mutations."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.config import (
    codegen_revision,
    github_app_id,
    github_app_private_key,
)
from app.models.execution import PublicationStage
from app.github.app_auth import build_app_jwt
from app.safety.killswitch import automation_enabled
from app.store import connections as connections_store
from app.store.llm_credentials import (
    CredentialCipher,
    CredentialConfigurationError,
)

CapabilityState = Literal["available", "disabled"]
CheckState = Literal["ready", "blocked"]
CapabilityReason = Literal[
    "rollout_stage_blocked",
    "automation_disabled",
    "repository_grant_missing",
    "github_app_unconfigured",
    "provider_unconfigured",
    "worker_unavailable",
    "runtime_unavailable",
]

_PUBLICATION_STAGES = frozenset(
    {
        PublicationStage.development_pr,
        PublicationStage.tenant_draft_pr,
    }
)
_RUNTIME_PROBE_TTL_SECONDS = 5.0
_RUNTIME_PROBE_STATE_ATTRIBUTE = "_changeset_runtime_probe_state"


class CapabilityChecks(BaseModel):
    """Exact prerequisites required by the changeset creation path."""

    model_config = ConfigDict(extra="forbid")

    rollout_stage: CheckState
    automation: CheckState
    repository_grant: CheckState
    github_app: CheckState
    provider: CheckState
    worker: CheckState
    runtime: CheckState


class ProjectLlmAssignmentCapability(BaseModel):
    """Secret-free current assignment metadata for the authenticated project."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["editor", "helper"]
    provider: Literal["anthropic", "openai", "google", "xai"]
    model_id: str
    connection_state: Literal["active"]


class ChangesetCreationCapability(BaseModel):
    """Authenticated project-specific capability response."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    changeset_creation: CapabilityState
    reasons: list[CapabilityReason]
    checks: CapabilityChecks
    llm_assignments: list[ProjectLlmAssignmentCapability]


@dataclass(frozen=True)
class CapabilityEvaluation:
    report: ChangesetCreationCapability
    connection: Any | None


@dataclass(frozen=True)
class _RuntimeProbeKey:
    # editor_identity is id(editor), a memory address CPython reuses after
    # collection. It is only unique because every entry stored under a key
    # holds a strong reference to that exact editor (see the two classes
    # below), so the address cannot be handed to another object while the
    # entry lives. Both readers additionally re-check identity with `is`
    # before trusting an entry, so an unexpected collision re-probes instead
    # of answering for the wrong runtime.
    editor_identity: int
    stage: PublicationStage
    revision: str


@dataclass(frozen=True)
class _RuntimeProbeResult:
    # Load-bearing: this strong reference is what keeps id(editor) unique for
    # the cached key. Weakening it silently reintroduces address reuse, and a
    # capability probe that answers "ready" for a different runtime is exactly
    # the fail-open this cache exists to avoid.
    editor: Any
    ready: bool
    expires_at: float


@dataclass(frozen=True)
class _RuntimeProbeInFlight:
    # Strong by the same contract as _RuntimeProbeResult.editor.
    editor: Any
    task: asyncio.Task[bool]


@dataclass
class _RuntimeProbeState:
    results: dict[_RuntimeProbeKey, _RuntimeProbeResult] = field(
        default_factory=dict
    )
    in_flight: dict[_RuntimeProbeKey, _RuntimeProbeInFlight] = field(
        default_factory=dict
    )


def _provider_configured() -> bool:
    try:
        CredentialCipher.from_environment()
    except CredentialConfigurationError:
        return False
    return True


def _provider_encryption_key_id() -> str | None:
    """Return the non-secret identity of the active credential key."""
    try:
        return CredentialCipher.from_environment().key_id
    except CredentialConfigurationError:
        return None


async def _project_llm_assignments(
    pool: Any,
    project_id: str,
    encryption_key_id: str | None,
) -> list[ProjectLlmAssignmentCapability]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT assignment.role, assignment.provider, assignment.model_id,
                   connection.state AS connection_state
            FROM codegen_project_model_assignments AS assignment
            JOIN codegen_project_provider_connections AS connection
              ON connection.project_id = assignment.project_id
             AND connection.provider = assignment.provider
             AND connection.version = assignment.connection_version
             AND connection.inventory_version = assignment.inventory_version
             AND connection.catalog_version = assignment.catalog_version
             AND connection.state = 'active'
            JOIN codegen_project_provider_models AS model
              ON model.project_id = assignment.project_id
             AND model.provider = assignment.provider
             AND model.model_id = assignment.model_id
             AND model.connection_version = connection.version
             AND model.inventory_version = connection.inventory_version
             AND model.catalog_version = connection.catalog_version
             AND assignment.role = ANY(model.supported_roles)
            JOIN codegen_project_provider_credentials AS credential
              ON credential.credential_id = connection.credential_id
             AND credential.project_id = connection.project_id
             AND credential.provider = connection.provider
             AND credential.state = 'active'
             AND credential.encryption_key_id = $2
            WHERE assignment.project_id = $1
            ORDER BY CASE assignment.role WHEN 'editor' THEN 0 ELSE 1 END
            """,
            project_id,
            encryption_key_id,
        )
    return [
        ProjectLlmAssignmentCapability(
            role=str(row["role"]),
            provider=str(row["provider"]),
            model_id=str(row["model_id"]),
            connection_state="active",
        )
        for row in rows
    ]


def _github_app_configured() -> bool:
    app_id = github_app_id().strip()
    private_key = github_app_private_key().strip()
    if re.fullmatch(r"[1-9][0-9]*", app_id) is None or not private_key:
        return False
    try:
        build_app_jwt(app_id, private_key)
    except Exception:  # PyJWT/cryptography expose backend-specific key errors.
        return False
    return True


def _worker_dependencies(app: Any) -> dict[str, Any] | None:
    dependencies = getattr(app.state, "job_deps", None)
    if not isinstance(dependencies, dict):
        return None
    required = (
        "editor",
        "mint_read_token",
        "mint_write_token",
        "mint_pr_write_token",
        "branch_publisher",
        "open_pr",
        "find_pr",
        "close_pr",
        "publication_gate",
    )
    if any(dependencies.get(name) is None for name in required):
        return None
    return dependencies


def _assert_runtime_ready(
    editor: Any,
    stage: PublicationStage,
    revision: str,
) -> None:
    if stage is PublicationStage.development_pr:
        editor.assert_runtime_ready(
            expected_revision=revision,
            require_immutable_image=False,
            require_egress_policy=False,
        )
        return
    editor.assert_runtime_ready(expected_revision=revision)


def _runtime_probe_state(app: Any) -> _RuntimeProbeState:
    state = getattr(app.state, _RUNTIME_PROBE_STATE_ATTRIBUTE, None)
    if isinstance(state, _RuntimeProbeState):
        return state
    state = _RuntimeProbeState()
    setattr(app.state, _RUNTIME_PROBE_STATE_ATTRIBUTE, state)
    return state


async def _run_runtime_probe(
    state: _RuntimeProbeState,
    key: _RuntimeProbeKey,
    editor: Any,
) -> bool:
    try:
        try:
            await asyncio.to_thread(
                _assert_runtime_ready,
                editor,
                key.stage,
                key.revision,
            )
        except (OSError, RuntimeError, ValueError):
            ready = False
        else:
            ready = True
        state.results[key] = _RuntimeProbeResult(
            editor=editor,
            ready=ready,
            expires_at=monotonic() + _RUNTIME_PROBE_TTL_SECONDS,
        )
        return ready
    finally:
        current = state.in_flight.get(key)
        if current is not None and current.task is asyncio.current_task():
            del state.in_flight[key]


async def _runtime_ready(
    app: Any,
    editor: Any,
    stage: PublicationStage,
    revision: str,
) -> bool:
    """Return a short-lived, single-flight probe result for one exact runtime."""
    state = _runtime_probe_state(app)
    now = monotonic()
    expired = [
        key
        for key, result in state.results.items()
        if result.expires_at <= now
    ]
    for key in expired:
        del state.results[key]

    key = _RuntimeProbeKey(
        editor_identity=id(editor),
        stage=stage,
        revision=revision,
    )
    cached = state.results.get(key)
    if cached is not None and cached.editor is editor:
        return cached.ready

    in_flight = state.in_flight.get(key)
    if in_flight is None or in_flight.editor is not editor:
        task = asyncio.create_task(_run_runtime_probe(state, key, editor))
        in_flight = _RuntimeProbeInFlight(editor=editor, task=task)
        state.in_flight[key] = in_flight
    return await asyncio.shield(in_flight.task)


async def evaluate_changeset_creation(
    app: Any,
    pool: Any,
    project_id: str,
) -> CapabilityEvaluation:
    """Re-evaluate every prerequisite for one project without optimistic fallbacks."""
    stage = getattr(app.state, "codegen_rollout_stage", None)
    stage_ready = (
        isinstance(stage, PublicationStage) and stage in _PUBLICATION_STAGES
    )
    automation_ready = automation_enabled(project_id)
    connection = await connections_store.get_connection(pool, project_id)
    github_ready = _github_app_configured()
    encryption_key_id = _provider_encryption_key_id()
    credential_store_ready = (
        _provider_configured() and encryption_key_id is not None
    )
    assignments = await _project_llm_assignments(
        pool,
        project_id,
        encryption_key_id,
    )
    assignments_ready = [item.role for item in assignments] == [
        "editor",
        "helper",
    ]
    provider_ready = credential_store_ready and assignments_ready
    dependencies = _worker_dependencies(app)
    worker_ready = dependencies is not None
    runtime_ready = False
    if stage_ready and dependencies is not None:
        runtime_ready = await _runtime_ready(
            app,
            dependencies["editor"],
            stage,
            codegen_revision(),
        )

    states: tuple[tuple[CapabilityReason, bool], ...] = (
        ("rollout_stage_blocked", stage_ready),
        ("automation_disabled", automation_ready),
        ("repository_grant_missing", connection is not None),
        ("github_app_unconfigured", github_ready),
        ("provider_unconfigured", provider_ready),
        ("worker_unavailable", worker_ready),
        ("runtime_unavailable", runtime_ready),
    )
    reasons = [reason for reason, ready in states if not ready]
    report = ChangesetCreationCapability(
        project_id=project_id,
        changeset_creation="disabled" if reasons else "available",
        reasons=reasons,
        checks=CapabilityChecks(
            rollout_stage="ready" if stage_ready else "blocked",
            automation="ready" if automation_ready else "blocked",
            repository_grant="ready" if connection is not None else "blocked",
            github_app="ready" if github_ready else "blocked",
            provider="ready" if provider_ready else "blocked",
            worker="ready" if worker_ready else "blocked",
            runtime="ready" if runtime_ready else "blocked",
        ),
        llm_assignments=assignments,
    )
    return CapabilityEvaluation(report=report, connection=connection)
