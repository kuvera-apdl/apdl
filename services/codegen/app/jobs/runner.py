"""Changeset job runner — drives a changeset through its lifecycle.

Lifecycle path: ``queued → cloning → editing → pushing → pr_open``. Generation,
review, or safety failures move directly to ``error``; CI is a separate external
projection. The edit itself is delegated to an :class:`~app.editor.base.Editor`
(Aider in production, a fake in tests); the PR is opened by codegen via
the GitHub App; GitHub owns verification and merge. The job never raises — any
unexpected fault lands the changeset in ``error``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.config import codegen_max_concurrent_jobs
from app.editor.base import Editor, EditRequest
from app.models.changeset import ChangesetStatus, InvalidTransition, TaskSpec
from app.models.observations import ExternalCIStatus, PullRequestObservation
from app.requirements import compile_requirement_ledger, map_implementation_evidence
from app.requirements.models import ImplementationStatus, RequirementLedger
from app.runtime.github_actions import workflow_attestation_is_valid
from app.runtime.models import RuntimeAcceptancePlan, RuntimeAcceptancePolicy
from app.safety.gates import evaluate_pre_push
from app.safety.killswitch import automation_enabled
from app.semantic_review.models import ReviewDecision, ReviewVerdict
from app.store import changesets as store
from app.store import connections as connections_store
from app.verification.models import PlanDisposition, VerificationCoverage, VerificationPlan

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


def _pr_body(
    task: TaskSpec,
    ledger: RequirementLedger,
    plan: VerificationPlan | None,
    coverage: VerificationCoverage | None,
    runtime_plan: RuntimeAcceptancePlan | None,
    review: ReviewVerdict | None,
) -> str:
    checks = "\n".join(f"- [ ] {c}" for c in task.constraints)
    if not checks:
        checks = "- [ ] Implements the described change with passing tests"
    requirement_lines = []
    for requirement in ledger.requirements:
        expected = ", ".join(
            item.evidence_id for item in requirement.expected_ci_evidence
        ) or "explicitly blocked/descoped"
        marker = (
            "x"
            if requirement.implementation_status
            in {ImplementationStatus.implemented, ImplementationStatus.confirmed_existing}
            else " "
        )
        requirement_lines.append(
            f"- [{marker}] `{requirement.requirement_id}` "
            f"{requirement.observable_behavior} (expected: {expected})"
        )
    ledger_text = "\n".join(requirement_lines)
    verification_text = (
        f"- Plan: `{plan.disposition.value}` — {plan.disposition_reason}\n"
        if plan is not None
        else "- Plan unavailable for this editor implementation.\n"
    )
    if coverage is not None:
        verification_text += (
            f"- Coverage: `{coverage.disposition.value}` — "
            f"{coverage.disposition_reason}\n"
        )
    verification_text += (
        "- These are expected-coverage facts only; GitHub CI is authoritative."
    )
    if runtime_plan is None:
        runtime_text = "- Runtime acceptance plan unavailable."
    else:
        runtime_text = (
            f"- Planned runtime checks: {len(runtime_plan.checks)}\n"
            f"- Explicit runtime blockers: {len(runtime_plan.blockers)}\n"
            "- Runtime evidence is produced and judged by GitHub Actions; missing "
            "artifacts remain unverified."
        )
    review_text = (
        f"- Decision: `{review.overall_decision.value}`\n"
        f"- Reviewed diff SHA-256: `{review.reviewed_diff_sha256}`\n"
        "- This pre-push semantic judgment is not a GitHub CI result."
        if review is not None
        else "- No semantic-review verdict was produced by this editor."
    )
    return (
        f"## Summary\n\n- {task.title}\n\n{task.spec}\n\n"
        f"## Requirement ledger\n\n{ledger_text}\n\n"
        f"## Verification coverage\n\n{verification_text}\n\n"
        f"## Runtime acceptance\n\n{runtime_text}\n\n"
        f"## Semantic review\n\n{review_text}\n\n"
        f"## Test plan\n\n{checks}\n\n"
        "## Notes\n\n"
        "- Opened automatically by APDL codegen from an approved feature proposal. "
        "GitHub CI, review rules, and merge controls are authoritative.\n"
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
        runtime_policy = RuntimeAcceptancePolicy.model_validate(
            policy.get("runtime_acceptance") or {}
        )
        gates_policy = dict(policy.get("gates") or {})
        revert_sha = changeset.task.context.get("revert_sha")
        result = await editor.implement(
            EditRequest(
                repo=connection.repo,
                project_scope=changeset.project_id,
                base_branch=base_branch,
                branch=branch,
                token=token,
                title=changeset.task.title,
                spec=changeset.task.spec,
                constraints=changeset.task.constraints,
                test_cmd=policy.get("test_cmd"),
                gates_policy=gates_policy,
                runtime_acceptance_policy=runtime_policy,
                revert_sha=revert_sha if isinstance(revert_sha, str) else None,
                risk_level=str(changeset.task.context.get("risk_level") or "low").lower(),
            )
        )

        # Protocol backstop for custom editors: the service, not an editor
        # implementation, owns the canonical ledger boundary.
        if result.requirement_ledger is None:
            compiled = compile_requirement_ledger(
                title=changeset.task.title,
                spec=changeset.task.spec,
                constraints=changeset.task.constraints,
                risk=str(changeset.task.context.get("risk_level") or "low").lower(),
                verification_command=policy.get("test_cmd"),
            )
            if result.success:
                compiled = map_implementation_evidence(
                    compiled, result.changed_paths or ["generated-change"]
                )
            result.requirement_ledger = compiled

        if result.contract_bundle is not None:
            await store.set_contract_bundle(pool, changeset_id, result.contract_bundle)
        if result.requirement_ledger is not None:
            await store.set_requirement_ledger(
                pool, changeset_id, result.requirement_ledger
            )
        if result.inspection_snapshot is not None or result.dependency_slice is not None:
            await store.set_inspection_evidence(
                pool,
                changeset_id,
                snapshot=result.inspection_snapshot,
                dependency_slice=result.dependency_slice,
            )
        if result.verification_plan is not None or result.verification_coverage is not None:
            await store.set_verification_evidence(
                pool,
                changeset_id,
                plan=result.verification_plan,
                coverage=result.verification_coverage,
            )
        if result.runtime_acceptance_plan is not None:
            await store.set_runtime_acceptance_plan(
                pool, changeset_id, result.runtime_acceptance_plan
            )
        if result.review_verdict is not None:
            await store.set_review_verdict(pool, changeset_id, result.review_verdict)

        # Persist the prompt transcript regardless of outcome — a failed run is
        # exactly when an operator wants to see what the model was told.
        # Best-effort: a transcript write must never sink an otherwise good run.
        if result.prompts:
            try:
                await store.set_prompts(pool, changeset_id, result.prompts)
            except Exception:
                logger.warning(
                    "Could not persist the prompt transcript for changeset %s",
                    changeset_id,
                    exc_info=True,
                )

        if not result.success:
            await store.transition_changeset(
                pool, changeset_id, ChangesetStatus.error,
                error=result.error or "The edit attempt did not pass tests.",
            )
            return
        if (
            result.requirement_ledger is None
            or not result.requirement_ledger.ready_for_pull_request()
        ):
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error=(
                    "Editor returned no complete RequirementLedger; no pull request "
                    "was created."
                ),
            )
            return
        if (
            result.review_verdict is not None
            and result.review_verdict.overall_decision is ReviewDecision.rejected
        ):
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error=(
                    "Semantic review rejected the change: "
                    + "; ".join(result.review_verdict.actionable_instructions)
                ),
            )
            return

        # Backstop only: the editor already ran these gates on the FULL diff
        # before pushing. This re-check (on the possibly capped diff_text)
        # guards editors that don't gate themselves (e.g. a fake/custom Editor).
        backstop_policy = dict(gates_policy)
        if workflow_attestation_is_valid(
            result.generated_runtime_workflow,
            plan=result.runtime_acceptance_plan,
            policy=runtime_policy,
        ):
            allowed = set(backstop_policy.get("allowed_protected_paths") or [])
            allowed.add(result.generated_runtime_workflow.path)
            backstop_policy["allowed_protected_paths"] = sorted(allowed)
        gate = evaluate_pre_push(
            diff_stat=result.diff_stat,
            changed_paths=result.changed_paths,
            diff_text=result.diff_text,
            policy=backstop_policy,
        )
        if not gate.passed:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error="Pre-push gate failed: " + "; ".join(gate.violations),
            )
            return

        if not result.head_sha:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error="Editor pushed a branch without returning its exact head SHA.",
            )
            return

        await store.transition_changeset(pool, changeset_id, ChangesetStatus.pushing)
        risk = str(changeset.task.context.get("risk_level") or "low").lower()
        lacks_external_ci = (
            result.verification_plan is not None
            and result.verification_plan.disposition
            is PlanDisposition.unverified_external_ci
        )
        semantic_unverified = (
            result.review_verdict is not None
            and result.review_verdict.overall_decision.value == "unverified"
        )
        runtime_unverified = bool(
            result.runtime_acceptance_plan
            and result.runtime_acceptance_plan.blockers
        )
        draft = (
            risk != "low"
            or lacks_external_ci
            or semantic_unverified
            or runtime_unverified
        )
        pr = await open_pr(
            repo=connection.repo,
            head=result.branch or branch,
            base=base_branch,
            title=changeset.task.title,
            body=_pr_body(
                changeset.task,
                result.requirement_ledger,
                result.verification_plan,
                result.verification_coverage,
                result.runtime_acceptance_plan,
                result.review_verdict,
            ),
            token=token,
            draft=draft,
        )
        if not pr.head_sha or pr.head_sha != result.head_sha:
            raise RuntimeError(
                "GitHub PR head does not match the exact branch head pushed by codegen."
            )
        external_ci_status = (
            ExternalCIStatus.unverified_external_ci
            if lacks_external_ci
            else ExternalCIStatus.pending
        )
        observation = PullRequestObservation(
            observation_id=f"probs_{uuid.uuid4().hex}",
            changeset_id=changeset_id,
            repository=connection.repo,
            pr_number=pr.number,
            head_sha=pr.head_sha,
            status=pr.status,
            action="opened",
            github_url=pr.url,
            github_updated_at=pr.github_updated_at,
            observed_at=datetime.now(timezone.utc),
        )
        await store.mark_pr_open(
            pool,
            changeset_id,
            branch=result.branch or branch,
            observation=observation,
            external_ci_status=external_ci_status,
            diff_stat=result.diff_stat,
        )
        logger.info("Changeset %s opened GitHub PR %s", changeset_id, pr.url)
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
