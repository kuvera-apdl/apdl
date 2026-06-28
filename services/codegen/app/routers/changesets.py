"""Changeset lifecycle endpoints.

Phase 1 scope: create (enqueue), read, list, and abandon. The sandboxed job
that drives ``queued → … → merged`` is wired in later phases; the seam is
:func:`_enqueue_job`. Merge (``POST /{id}/merge``) arrives with CI gating in a
later phase and is intentionally absent here rather than stubbed to lie.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from app.auth import require_internal_token
from app.github.app_auth import mint_token_for_repo
from app.github.pulls import close_pull_request, merge_pull_request
from app.jobs.runner import run_changeset_job
from app.models.changeset import (
    Changeset,
    ChangesetCreate,
    ChangesetStatus,
    InvalidTransition,
    MergeRequest,
)
from app.store import changesets as store
from app.store import connections as connections_store

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/changesets",
    tags=["changesets"],
    dependencies=[Depends(require_internal_token)],
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


@router.post("", response_model=Changeset, status_code=202)
async def create_changeset(
    body: ChangesetCreate, request: Request, background_tasks: BackgroundTasks
) -> Changeset:
    """Enqueue a changeset for a connected project."""
    pool: asyncpg.Pool = request.app.state.pg_pool

    connection = await connections_store.get_connection(pool, body.project_id)
    if connection is None:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{body.project_id}' has no connected repository.",
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
    return await store.list_changesets(pool, project_id, limit)


@router.get("/{changeset_id}", response_model=Changeset)
async def get_changeset(changeset_id: str, request: Request) -> Changeset:
    """Fetch one changeset by id."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    changeset = await store.get_changeset(pool, changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    return changeset


async def _close_pr_best_effort(pool: asyncpg.Pool, changeset: Changeset) -> None:
    """Close an abandoned changeset's PR; swallow GitHub failures (logged).

    The DB transition is the source of truth — abandon must never be blocked on
    GitHub being reachable, so any failure here is logged and the PR is left open
    rather than raised. The head branch is intentionally not deleted.
    """
    try:
        connection = await connections_store.get_connection(pool, changeset.project_id)
        if connection is None:
            logger.warning(
                "Abandoned changeset %s has no repo connection; PR #%s left open.",
                changeset.changeset_id,
                changeset.pr_number,
            )
            return
        token = (
            await mint_token_for_repo(connection.installation_id, connection.repo)
        ).token
        await close_pull_request(
            repo=connection.repo, number=changeset.pr_number, token=token
        )
        logger.info(
            "Closed PR #%s for abandoned changeset %s.",
            changeset.pr_number,
            changeset.changeset_id,
        )
    except Exception:
        logger.warning(
            "Could not close PR #%s for abandoned changeset %s; left open on GitHub.",
            changeset.pr_number,
            changeset.changeset_id,
            exc_info=True,
        )


@router.post("/{changeset_id}/abandon", response_model=Changeset)
async def abandon_changeset(changeset_id: str, request: Request) -> Changeset:
    """Abandon a changeset and close its open PR (best-effort).

    Rollback for an un-merged change: the DB status moves to ``abandoned`` and,
    if a PR was opened, it is closed on GitHub. Closing is best-effort (a GitHub
    failure is logged, not raised) and the head branch is left in place.
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    try:
        changeset = await store.transition_changeset(
            pool, changeset_id, ChangesetStatus.abandoned
        )
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    if changeset.pr_number is not None:
        await _close_pr_best_effort(pool, changeset)
    return changeset


@router.post("/{changeset_id}/merge", response_model=Changeset)
async def merge_changeset(
    changeset_id: str, body: MergeRequest, request: Request
) -> Changeset:
    """Merge a changeset's PR. Green CI is mandatory; APDL gates the decision."""
    pool: asyncpg.Pool = request.app.state.pg_pool
    changeset = await store.get_changeset(pool, changeset_id)
    if changeset is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
    if changeset.status not in (ChangesetStatus.ci_passed, ChangesetStatus.waiting_approval):
        raise HTTPException(
            status_code=409,
            detail=f"Changeset is '{changeset.status.value}', not mergeable.",
        )
    # "passed" = CI green; "none" = the repo has no CI configured, so there is no
    # gate to clear (sync_ci_status records this). Any other value (pending /
    # failed / unset) still blocks the merge.
    if changeset.ci_status not in ("passed", "none"):
        raise HTTPException(status_code=409, detail="Merge requires green CI.")
    if changeset.pr_number is None:
        raise HTTPException(status_code=409, detail="Changeset has no open pull request.")

    connection = await connections_store.get_connection(pool, changeset.project_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Repository connection is missing.")

    token = (
        await mint_token_for_repo(connection.installation_id, connection.repo)
    ).token
    merge = await merge_pull_request(
        repo=connection.repo,
        number=changeset.pr_number,
        token=token,
        merge_method=body.merge_method,
    )
    if not merge.merged:
        # Not-mergeable (conflict / unmet checks / head moved) is a client-state
        # 409, not a 502 — merge_pull_request already maps GitHub's 405/409/422
        # refusals to this clean result instead of letting them surface as a 500.
        raise HTTPException(
            status_code=409,
            detail=merge.reason or "GitHub declined the merge (the PR is not mergeable).",
        )

    return await store.transition_changeset(pool, changeset_id, ChangesetStatus.merged)


@router.post("/{changeset_id}/revert", response_model=Changeset, status_code=202)
async def revert_changeset(
    changeset_id: str, request: Request, background_tasks: BackgroundTasks
) -> Changeset:
    """Roll back a MERGED changeset by opening a revert PR.

    Reuses the changeset pipeline: a new changeset is enqueued whose task is to
    revert the original PR. (Un-merged changes roll back via /abandon instead.)
    """
    pool: asyncpg.Pool = request.app.state.pg_pool
    original = await store.get_changeset(pool, changeset_id)
    if original is None:
        raise HTTPException(status_code=404, detail=f"Changeset '{changeset_id}' not found.")
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
    revert_task = {
        "title": f"Revert: {original.task.title}",
        "spec": (
            f"Revert pull request {pr_ref} (branch `{original.branch}`), which was "
            f"merged into `{base}`. Produce a clean revert of all its changes and "
            "keep the test suite green."
        ),
        "context": {
            "reverts_changeset": changeset_id,
            "reverts_pr_number": original.pr_number,
        },
        "constraints": ["All existing tests must pass."],
    }
    new_changeset = await store.create_changeset(
        pool,
        changeset_id=new_id,
        project_id=original.project_id,
        run_id=original.run_id,
        base_branch=base,
        task=revert_task,
    )
    _maybe_enqueue(request.app, background_tasks, new_id)
    return new_changeset
