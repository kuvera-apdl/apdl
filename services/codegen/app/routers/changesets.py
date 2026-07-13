"""Changeset lifecycle endpoints.

APDL creates and manages changeset work, but GitHub owns CI verification and
merge. There is intentionally no merge endpoint.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from app.auth import require_project
from app.evaluations.models import RolloutStage
from app.jobs.runner import run_changeset_job
from app.models.changeset import (
    RETRYABLE_STATUSES,
    Changeset,
    ChangesetCreate,
    ChangesetStatus,
    InvalidTransition,
)
from app.models.observations import ChangesetObservationHistory
from app.runtime.models import RuntimeEvidenceObservation
from app.safety.policy import resolve_effective_policy
from app.store import changesets as store
from app.store import connections as connections_store
from app.store import observations as observation_store
from app.store import runtime_evidence as runtime_evidence_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/changesets",
    tags=["changesets"],
)


def _require_publication_stage(app: Any) -> None:
    """Reject production work while this deployment is evaluation-only."""
    stage = getattr(app.state, "codegen_rollout_stage", None)
    if stage in {RolloutStage.offline, RolloutStage.shadow}:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Codegen is configured for the '{stage.value}' evaluation stage; "
                "GitHub branch and pull-request publication are disabled."
            ),
        )


def _maybe_enqueue(app: Any, background_tasks: BackgroundTasks, changeset_id: str) -> None:
    """Schedule the changeset job when the runner deps are configured.

    Lifespan wires ``app.state.job_deps`` (editor + token minter + PR opener). In
    tests the lifespan does not run, so the deps are absent and the changeset
    simply parks in ``queued`` — the job is exercised directly in unit tests.
    """
    deps = getattr(app.state, "job_deps", None)
    if deps is None:
        logger.info("Changeset %s queued; job runner not configured.", changeset_id)
        return
    background_tasks.add_task(run_changeset_job, app.state.pg_pool, changeset_id, **deps)


def _policy_provenance(app: Any, connection: Any) -> tuple[Any, str]:
    """Capture tenant preferences and the effective policy active at enqueue time."""
    platform = getattr(app.state, "platform_codegen_safety_policy", None)
    tenant = connection.tenant_policy
    effective = resolve_effective_policy(tenant, platform)
    return tenant, effective.canonical_digest()


async def _current_connection(pool: asyncpg.Pool, project_id: str) -> Any:
    connection = await connections_store.get_connection(pool, project_id)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_id}' has no connected repository.",
        )
    return connection


async def _authorized_changeset(
    pool: asyncpg.Pool,
    request: Request,
    changeset_id: str,
    role: str,
) -> Changeset:
    """Resolve one changeset and bind its project to the verified principal."""
    changeset = await store.get_changeset(pool, changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    require_project(request, changeset.project_id, role)
    return changeset


@router.post("", response_model=Changeset, status_code=202)
async def create_changeset(
    body: ChangesetCreate, request: Request, background_tasks: BackgroundTasks
) -> Changeset:
    """Enqueue a changeset for a connected project."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, body.project_id, "agents:manage")
    _require_publication_stage(request.app)

    connection = await _current_connection(pool, body.project_id)
    tenant_policy, effective_policy_sha256 = _policy_provenance(
        request.app, connection
    )

    changeset_id = f"cs_{uuid.uuid4().hex[:24]}"
    base_branch = body.base_branch or connection.default_base_branch
    changeset = await store.create_changeset(
        pool,
        changeset_id=changeset_id,
        project_id=body.project_id,
        run_id=body.run_id,
        base_branch=base_branch,
        task=body.task.model_dump(),
        repository_target=connection.target,
        tenant_policy_snapshot=tenant_policy,
        effective_safety_policy_sha256=effective_policy_sha256,
    )
    _maybe_enqueue(request.app, background_tasks, changeset_id)
    return changeset


@router.get("", response_model=list[Changeset])
async def list_changesets(
    request: Request,
    project_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
) -> list[Changeset]:
    """List a project's changesets, most recent first."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    require_project(request, project_id, "agents:read")
    return await store.list_changesets(pool, project_id, limit)


@router.get("/{changeset_id}", response_model=Changeset)
async def get_changeset(changeset_id: str, request: Request) -> Changeset:
    """Fetch one changeset by id."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    return await _authorized_changeset(pool, request, changeset_id, "agents:read")


@router.post("/{changeset_id}/abandon", response_model=Changeset)
async def abandon_changeset(changeset_id: str, request: Request) -> Changeset:
    """Abandon only queued pre-PR work; open PRs are controlled on GitHub."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    current = await _authorized_changeset(
        pool, request, changeset_id, "agents:manage"
    )
    if current.pr_number is not None:
        raise HTTPException(
            status_code=409,
            detail="Open or closed pull requests must be managed on GitHub.",
        )
    try:
        changeset = await store.transition_changeset(
            pool, changeset_id, ChangesetStatus.abandoned
        )
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    return changeset


@router.get(
    "/{changeset_id}/observations",
    response_model=ChangesetObservationHistory,
)
async def get_changeset_observations(
    changeset_id: str, request: Request
) -> ChangesetObservationHistory:
    """Return immutable PR, exact-head CI, and remediation journals."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    await _authorized_changeset(pool, request, changeset_id, "agents:read")
    return ChangesetObservationHistory(
        pull_requests=await observation_store.list_pull_request_observations(
            pool, changeset_id, limit=200
        ),
        ci_verifications=await observation_store.list_ci_verification_observations(
            pool, changeset_id, limit=200
        ),
        remediation_attempts=await observation_store.list_ci_remediation_attempts(
            pool, changeset_id, limit=200
        ),
    )


@router.get(
    "/{changeset_id}/runtime-observations",
    response_model=list[RuntimeEvidenceObservation],
)
async def get_runtime_observations(
    changeset_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> list[RuntimeEvidenceObservation]:
    """Return append-only GitHub Actions runtime evidence for every PR head."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    await _authorized_changeset(pool, request, changeset_id, "agents:read")
    return await runtime_evidence_store.list_runtime_evidence_observations(
        pool, changeset_id, limit=limit
    )


@router.post("/{changeset_id}/revert", response_model=Changeset, status_code=202)
async def revert_changeset(
    changeset_id: str, request: Request, background_tasks: BackgroundTasks
) -> Changeset:
    """Roll back a MERGED changeset by opening a revert PR.

    Reuses the changeset pipeline: a new changeset is enqueued whose task is to
    revert the original PR. (Un-merged changes roll back via /abandon instead.)
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    original = await _authorized_changeset(
        pool, request, changeset_id, "agents:manage"
    )
    _require_publication_stage(request.app)
    if original.status != ChangesetStatus.merged:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only merged changesets can be reverted "
                f"(this one is '{original.status.value}'); use /abandon for un-merged work."
            ),
        )

    new_id = f"cs_{uuid.uuid4().hex[:24]}"
    base = original.base_branch or "main"
    pr_ref = f"#{original.pr_number}" if original.pr_number else f"branch `{original.branch}`"
    # ``revert_sha`` (recorded at merge time) makes the revert deterministic:
    # the editor runs ``git revert <sha>`` instead of asking the agent to
    # reconstruct the change from prose it cannot see in a shallow clone. A
    # changeset merged before the SHA was recorded falls back to the prose path.
    context: dict = {
        "reverts_changeset": changeset_id,
        "reverts_pr_number": original.pr_number,
    }
    sha_note = ""
    if original.merge_sha:
        context["revert_sha"] = original.merge_sha
        sha_note = f" The merge commit to revert is `{original.merge_sha}`."
    revert_task = {
        "title": f"Revert: {original.task.title}",
        "spec": (
            f"Revert pull request {pr_ref} (branch `{original.branch}`), which was "
            f"merged into `{base}`.{sha_note} Produce a clean revert of all its "
            "changes and keep the test suite green."
        ),
        "context": context,
        "constraints": ["All existing tests must pass."],
    }
    connection = await _current_connection(pool, original.project_id)
    tenant_policy, effective_policy_sha256 = _policy_provenance(
        request.app, connection
    )
    new_changeset = await store.create_changeset(
        pool,
        changeset_id=new_id,
        project_id=original.project_id,
        run_id=original.run_id,
        base_branch=base,
        task=revert_task,
        repository_target=connection.target,
        tenant_policy_snapshot=tenant_policy,
        effective_safety_policy_sha256=effective_policy_sha256,
    )
    _maybe_enqueue(request.app, background_tasks, new_id)
    return new_changeset


@router.post("/{changeset_id}/retry", response_model=Changeset, status_code=202)
async def retry_changeset(
    changeset_id: str, request: Request, background_tasks: BackgroundTasks
) -> Changeset:
    """Re-run a FAILED changeset as a fresh changeset with the same task.

    Only pre-PR generation errors may create a new changeset. CI failures repair
    the same GitHub PR branch, and closed PRs may only be reopened on GitHub.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    original = await _authorized_changeset(
        pool, request, changeset_id, "agents:manage"
    )
    _require_publication_stage(request.app)
    if original.status not in RETRYABLE_STATUSES:
        retryable = ", ".join(sorted(s.value for s in RETRYABLE_STATUSES))
        raise HTTPException(
            status_code=409,
            detail=(
                f"Only failed changesets can be retried (this one is "
                f"'{original.status.value}'; retryable: {retryable})."
            ),
        )
    if original.pr_number is not None:
        raise HTTPException(
            status_code=409,
            detail="A changeset with a GitHub PR cannot be retried as a replacement PR.",
        )

    new_id = f"cs_{uuid.uuid4().hex[:24]}"
    # Re-run the identical task; thread a retry_of marker through the task
    # context so the new changeset's lineage back to the failed run is traceable.
    task = original.task.model_dump()
    task["context"] = {**task.get("context", {}), "retry_of": changeset_id}
    connection = await _current_connection(pool, original.project_id)
    tenant_policy, effective_policy_sha256 = _policy_provenance(
        request.app, connection
    )
    new_changeset = await store.create_changeset(
        pool,
        changeset_id=new_id,
        project_id=original.project_id,
        run_id=original.run_id,
        base_branch=original.base_branch,
        task=task,
        repository_target=connection.target,
        tenant_policy_snapshot=tenant_policy,
        effective_safety_policy_sha256=effective_policy_sha256,
    )
    _maybe_enqueue(request.app, background_tasks, new_id)
    return new_changeset
