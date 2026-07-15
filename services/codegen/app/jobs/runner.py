"""Changeset job runner — drives a changeset through its lifecycle.

Phase 2 path: ``queued → cloning → editing → testing → (tests_failed | pushing →
pr_open)``. The edit itself is delegated to an :class:`~app.editor.base.Editor`
(Aider in production, a fake in tests); the PR is opened by codegen via
the GitHub App so merge gating stays in APDL. The job never raises — any
unexpected fault lands the changeset in ``error``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from app.config import codegen_max_concurrent_jobs
from app.editor.base import Editor, EditRequest
from app.models.changeset import ChangesetStatus, InvalidTransition, TaskSpec
from app.safety.gates import evaluate_pre_push
from app.safety.killswitch import automation_enabled
from app.store import changesets as store
from app.store import connections as connections_store

logger = logging.getLogger(__name__)

TokenMinter = Callable[[int, str], Awaitable[str]]
PROpener = Callable[..., Awaitable[Any]]

#: Serializes changeset jobs to the configured concurrency (default 1). Created
#: lazily so it binds to the running event loop; safe under a single-threaded
#: loop (no await between the None check and assignment).
#:
#: NB: this is a PER-PROCESS limit. It only bounds host load if the service runs
#: a single uvicorn worker — N workers each get their own semaphore, so effective
#: concurrency becomes N×limit. The Dockerfile pins ``--workers 1``; if that ever
#: changes, coordinate the slot out-of-process (Postgres advisory lock / DB
#: running-count) instead of relying on this.
_job_semaphore: asyncio.Semaphore | None = None


def _job_slot() -> asyncio.Semaphore:
    global _job_semaphore
    if _job_semaphore is None:
        _job_semaphore = asyncio.Semaphore(codegen_max_concurrent_jobs())
    return _job_semaphore


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:40] or "change"


def _pr_body(task: TaskSpec) -> str:
    checks = "\n".join(f"- [ ] {c}" for c in task.constraints)
    if not checks:
        checks = "- [ ] Implements the described change with passing tests"
    return (
        f"## Summary\n\n- {task.title}\n\n{task.spec}\n\n"
        f"## Test plan\n\n{checks}\n\n"
        "## Notes\n\n"
        "- Opened automatically by APDL codegen from an approved feature proposal. "
        "Draft until CI is green; the merge decision is gated by APDL.\n"
    )


async def run_changeset_job(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    editor: Editor,
    mint_token: TokenMinter,
    open_pr: PROpener,
) -> None:
    """Run one changeset, gated by the concurrency slot.

    Excess jobs wait here (the changeset stays ``queued``) until a slot frees, so
    a small host never runs more coding-agent + build pipelines than it can take.
    """
    async with _job_slot():
        await _execute_changeset_job(
            pool, changeset_id, editor=editor, mint_token=mint_token, open_pr=open_pr
        )


async def _execute_changeset_job(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    editor: Editor,
    mint_token: TokenMinter,
    open_pr: PROpener,
) -> None:
    """Execute one changeset end-to-end (edit → push → open draft PR)."""
    changeset = await store.get_changeset(pool, changeset_id)
    if changeset is None:
        logger.warning("Changeset job for unknown id %s", changeset_id)
        return

    if not automation_enabled(changeset.project_id):
        await store.transition_changeset(
            pool,
            changeset_id,
            ChangesetStatus.abandoned,
            error="Code automation is disabled for this project (kill switch).",
        )
        return

    try:
        connection = await connections_store.get_connection(pool, changeset.project_id)
        if connection is None:
            await store.transition_changeset(
                pool, changeset_id, ChangesetStatus.error,
                error="Project repository connection is missing.",
            )
            return

        base_branch = changeset.base_branch or connection.default_base_branch
        # The queued → cloning transition doubles as the job's claim: it is
        # row-locked and state-machine-checked, so exactly one worker wins.
        # Losing the claim (a duplicate enqueue, a concurrent replica) is a
        # clean no-op, not an error that would corrupt the winner's run.
        try:
            await store.transition_changeset(pool, changeset_id, ChangesetStatus.cloning)
        except InvalidTransition:
            logger.info(
                "Changeset %s is already claimed by another job; skipping.",
                changeset_id,
            )
            return
        token = await mint_token(connection.installation_id, connection.repo)

        await store.transition_changeset(pool, changeset_id, ChangesetStatus.editing)
        branch = f"apdl/{_slug(changeset.task.title)}-{changeset_id[-8:]}"
        policy = connection.policy if isinstance(connection.policy, dict) else {}
        revert_sha = changeset.task.context.get("revert_sha")
        result = await editor.implement(
            EditRequest(
                repo=connection.repo,
                base_branch=base_branch,
                branch=branch,
                token=token,
                title=changeset.task.title,
                spec=changeset.task.spec,
                constraints=changeset.task.constraints,
                test_cmd=policy.get("test_cmd"),
                gates_policy=policy.get("gates"),
                revert_sha=revert_sha if isinstance(revert_sha, str) else None,
            )
        )

        await store.transition_changeset(pool, changeset_id, ChangesetStatus.testing)
        if not result.success:
            await store.transition_changeset(
                pool, changeset_id, ChangesetStatus.tests_failed,
                error=result.error or "The edit attempt did not pass tests.",
            )
            return

        # Backstop only: the editor already ran these gates on the FULL diff
        # before pushing. This re-check (on the possibly capped diff_text)
        # guards editors that don't gate themselves (e.g. a fake/custom Editor).
        gate = evaluate_pre_push(
            diff_stat=result.diff_stat,
            changed_paths=result.changed_paths,
            diff_text=result.diff_text,
            policy=policy.get("gates"),
        )
        if not gate.passed:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.tests_failed,
                error="Pre-push gate failed: " + "; ".join(gate.violations),
            )
            return

        await store.transition_changeset(pool, changeset_id, ChangesetStatus.pushing)
        pr = await open_pr(
            repo=connection.repo,
            head=result.branch or branch,
            base=base_branch,
            title=changeset.task.title,
            body=_pr_body(changeset.task),
            token=token,
            draft=True,
        )
        await store.mark_pr_open(
            pool, changeset_id,
            branch=result.branch or branch,
            pr_url=pr.url,
            pr_number=pr.number,
            diff_stat=result.diff_stat,
            node_id=pr.node_id,
        )
        logger.info("Changeset %s opened draft PR %s", changeset_id, pr.url)
    except Exception as exc:
        logger.exception("Changeset job %s failed", changeset_id)
        try:
            await store.transition_changeset(
                pool, changeset_id, ChangesetStatus.error, error=str(exc)
            )
        except Exception:
            logger.exception("Could not mark changeset %s errored", changeset_id)


async def run_stale_sweeper(
    pool: asyncpg.Pool,
    *,
    interval_seconds: int,
    older_than_seconds: int,
    error: str,
) -> None:
    """Periodically fail active-state changesets that stopped moving.

    The startup sweep only catches orphans that are already old enough when the
    process boots; a changeset orphaned shortly *before* a restart would
    otherwise sit in ``cloning``/``editing``/… until some much later restart.
    This loop re-runs the same sweep forever so any row past the deadline is
    surfaced within one interval of aging out. A live job never trips it as long
    as ``older_than_seconds`` exceeds the per-job pipeline budget.
    """
    logger.info(
        "Stale-changeset sweeper started (interval=%ss, deadline=%ss)",
        interval_seconds,
        older_than_seconds,
    )
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                swept = await store.fail_stale_changesets(
                    pool, older_than_seconds=older_than_seconds, error=error
                )
                if swept:
                    logger.warning(
                        "Swept %d stale changeset(s) to error: %s",
                        len(swept),
                        ", ".join(swept),
                    )
            except Exception:
                logger.warning(
                    "Stale-changeset sweep errored; retrying next interval",
                    exc_info=True,
                )
    except asyncio.CancelledError:
        logger.info("Stale-changeset sweeper stopped")
        raise
