"""Tenant-scoped executable capability checks for Codegen mutations."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.config import (
    codegen_revision,
    github_app_id,
    github_app_private_key,
)
from app.editor.environment import (
    ModelProviderConfigurationError,
    resolve_model_provider_environment,
)
from app.evaluations.models import RolloutStage
from app.github.app_auth import build_app_jwt
from app.safety.killswitch import automation_enabled
from app.store import connections as connections_store

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
        RolloutStage.development_pr,
        RolloutStage.reviewed_pr,
        RolloutStage.low_risk_canary,
    }
)


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


class ChangesetCreationCapability(BaseModel):
    """Authenticated project-specific capability response."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    changeset_creation: CapabilityState
    reasons: list[CapabilityReason]
    checks: CapabilityChecks


@dataclass(frozen=True)
class CapabilityEvaluation:
    report: ChangesetCreationCapability
    connection: Any | None


def _provider_configured() -> bool:
    try:
        resolve_model_provider_environment(os.environ)
    except ModelProviderConfigurationError:
        return False
    return True


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
    if any(name not in dependencies for name in required):
        return None
    return dependencies


def _assert_runtime_ready(editor: Any, stage: RolloutStage) -> None:
    if stage is RolloutStage.development_pr:
        editor.assert_runtime_ready(
            expected_revision=codegen_revision(),
            require_immutable_image=False,
            require_egress_policy=False,
        )
        return
    editor.assert_runtime_ready(expected_revision=codegen_revision())


async def evaluate_changeset_creation(
    app: Any,
    pool: Any,
    project_id: str,
) -> CapabilityEvaluation:
    """Re-evaluate every prerequisite for one project without optimistic fallbacks."""
    stage = getattr(app.state, "codegen_rollout_stage", None)
    stage_ready = isinstance(stage, RolloutStage) and stage in _PUBLICATION_STAGES
    automation_ready = automation_enabled(project_id)
    connection = await connections_store.get_connection(pool, project_id)
    github_ready = _github_app_configured()
    provider_ready = _provider_configured()
    dependencies = _worker_dependencies(app)
    worker_ready = dependencies is not None
    runtime_ready = False
    if stage_ready and dependencies is not None:
        try:
            await asyncio.to_thread(
                _assert_runtime_ready,
                dependencies["editor"],
                stage,
            )
        except (OSError, RuntimeError, ValueError):
            runtime_ready = False
        else:
            runtime_ready = True

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
    )
    return CapabilityEvaluation(report=report, connection=connection)
