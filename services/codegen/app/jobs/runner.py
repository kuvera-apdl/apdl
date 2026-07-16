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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
import asyncpg

from app.config import codegen_max_concurrent_jobs
from app.editor.base import Editor, EditRequest
from app.github.publisher import BranchPublisher
from app.jobs.pr_publication import (
    PRCloser,
    PRFinder,
    PROpener,
    resume_pull_request_publication,
)
from app.models.changeset import ChangesetStatus, InvalidTransition, TaskSpec
from app.models.observations import ExternalCIStatus
from app.models.pr_publication import PublicationIntentRecorded
from app.publication import (
    DevelopmentPublicationAuthorization,
    PublicationAuthorizationRecord,
    PublicationGate,
)
from app.requirements import compile_requirement_ledger, map_implementation_evidence
from app.requirements.models import ImplementationStatus, RequirementLedger
from app.runtime.github_actions import workflow_attestation_is_valid
from app.runtime.models import RuntimeAcceptancePlan, RuntimeAcceptancePolicy
from app.safety.gates import evaluate_pre_push
from app.safety.killswitch import automation_enabled
from app.safety.policy import (
    PlatformCodegenSafetyPolicy,
    VerifiedProtectedPathExemption,
    resolve_effective_policy,
)
from app.semantic_review.models import ReviewDecision, ReviewVerdict
from app.store import changesets as store
from app.store import connections as connections_store
from app.store import pr_publication as publication_store
from app.verification.models import (
    PlanDisposition,
    VerificationCoverage,
    VerificationPlan,
)

logger = logging.getLogger(__name__)

TokenMinter = Callable[[str], AbstractAsyncContextManager[str]]

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
    publication: PublicationAuthorizationRecord,
) -> str:
    checks = "\n".join(f"- [ ] {c}" for c in task.constraints)
    if not checks:
        checks = "- [ ] Implements the described change with passing tests"
    requirement_lines = []
    for requirement in ledger.requirements:
        expected = (
            ", ".join(item.evidence_id for item in requirement.expected_ci_evidence)
            or "explicitly blocked/descoped"
        )
        marker = (
            "x"
            if requirement.implementation_status
            in {
                ImplementationStatus.implemented,
                ImplementationStatus.confirmed_existing,
            }
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
    if isinstance(publication, DevelopmentPublicationAuthorization):
        publication_text = (
            f"- Stage: `{publication.request.requested_stage.value}`\n"
            f"- Model: `{publication.request.model}`\n"
            f"- Codegen revision: `{publication.request.codegen_revision}`\n"
            "- Authority: local development; no evaluation evidence is claimed.\n"
            f"- Authorization SHA-256: `{publication.authorization_sha256}`\n"
            "- This authorization always creates a draft PR. GitHub CI, review, "
            "and merge remain authoritative."
        )
    else:
        publication_text = (
            f"- Stage: `{publication.request.requested_stage.value}`\n"
            f"- Model: `{publication.request.model}`\n"
            f"- Codegen revision: `{publication.request.codegen_revision}`\n"
            f"- Evaluation report SHA-256: `{publication.report_sha256}`\n"
            f"- Authorization SHA-256: `{publication.authorization_sha256}`\n"
            "- This authorizes PR publication only; GitHub CI, review, and merge "
            "remain authoritative."
        )
    return (
        f"## Summary\n\n- {task.title}\n\n{task.spec}\n\n"
        f"## Requirement ledger\n\n{ledger_text}\n\n"
        f"## Verification coverage\n\n{verification_text}\n\n"
        f"## Runtime acceptance\n\n{runtime_text}\n\n"
        f"## Semantic review\n\n{review_text}\n\n"
        f"## Publication rollout\n\n{publication_text}\n\n"
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
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    mint_pr_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
    open_pr: PROpener,
    find_pr: PRFinder,
    close_pr: PRCloser,
    publication_gate: PublicationGate,
    platform_safety_policy: PlatformCodegenSafetyPolicy | None = None,
) -> None:
    """Run one changeset, gated by the concurrency slot.

    Excess jobs wait here (the changeset stays ``queued``) until a slot frees, so
    a small host never runs more coding-agent + build pipelines than it can take.
    """
    async with _job_slot():
        await _execute_changeset_job(
            pool,
            changeset_id,
            editor=editor,
            mint_read_token=mint_read_token,
            mint_write_token=mint_write_token,
            mint_pr_write_token=mint_pr_write_token,
            branch_publisher=branch_publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
            publication_gate=publication_gate,
            platform_safety_policy=platform_safety_policy,
        )


async def _execute_changeset_job(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    editor: Editor,
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    mint_pr_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
    open_pr: PROpener,
    find_pr: PRFinder,
    close_pr: PRCloser,
    publication_gate: PublicationGate,
    platform_safety_policy: PlatformCodegenSafetyPolicy | None,
) -> None:
    """Execute one changeset end-to-end (edit → push → open draft PR)."""
    publication_recovery_owned = False
    try:
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

        connection = await connections_store.get_connection_for_changeset(
            pool, changeset_id
        )
        if connection is None:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error="Changeset repository grant is unavailable or revoked.",
            )
            return
        tenant_policy = changeset.tenant_policy_snapshot or connection.tenant_policy
        effective_safety_policy = resolve_effective_policy(
            tenant_policy,
            platform_safety_policy,
        )
        runtime_policy = RuntimeAcceptancePolicy(
            enabled=effective_safety_policy.runtime_workflow_generation_enabled
        )
        base_branch = changeset.base_branch or connection.default_base_branch
        # The queued → cloning transition doubles as the job's claim: it is
        # row-locked and state-machine-checked, so exactly one worker wins.
        # Losing the claim (a duplicate enqueue, a concurrent replica) is a
        # clean no-op, not an error that would corrupt the winner's run.
        try:
            await store.transition_changeset(
                pool, changeset_id, ChangesetStatus.cloning
            )
        except InvalidTransition:
            logger.info(
                "Changeset %s is already claimed by another job; skipping.",
                changeset_id,
            )
            return
        await store.set_safety_policy_provenance(
            pool,
            changeset_id,
            tenant_policy_snapshot=tenant_policy,
            effective_safety_policy_sha256=(effective_safety_policy.canonical_digest()),
        )
        risk = changeset.controls.risk_level
        authorization = publication_gate.authorize(
            risk=risk,
            canary_identity=f"{changeset.project_id}:{connection.repository_id}",
        )
        await store.set_publication_authorization(pool, changeset_id, authorization)
        if not authorization.decision.allowed:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error=(
                    "Publication rollout denied before GitHub credential minting: "
                    + "; ".join(authorization.decision.reasons)
                ),
            )
            return
        if not (
            authorization.decision.publish_branch
            and authorization.decision.create_pull_request
        ):
            raise RuntimeError(
                "rollout authorization did not grant branch and PR publication"
            )

        await store.transition_changeset(pool, changeset_id, ChangesetStatus.editing)
        branch_identity = changeset_id.removeprefix("cs_")
        branch = f"apdl/{_slug(changeset.task.title)}-{branch_identity}"
        revert_sha = (
            changeset.controls.revert.merge_sha
            if changeset.controls.revert is not None
            else None
        )
        async with mint_read_token(changeset_id) as token:
            result = await editor.implement(
                EditRequest(
                    repo=connection.repository_full_name,
                    project_scope=changeset.project_id,
                    base_branch=base_branch,
                    branch=branch,
                    token=token,
                    title=changeset.task.title,
                    spec=changeset.task.spec,
                    constraints=changeset.task.constraints,
                    test_cmd=tenant_policy.test_cmd,
                    safety_policy=effective_safety_policy,
                    runtime_acceptance_policy=runtime_policy,
                    revert_sha=revert_sha,
                    risk_level=risk.value,
                )
            )

        # Protocol backstop for custom editors: the service, not an editor
        # implementation, owns the canonical ledger boundary.
        if result.requirement_ledger is None:
            compiled = compile_requirement_ledger(
                title=changeset.task.title,
                spec=changeset.task.spec,
                constraints=changeset.task.constraints,
                risk=risk.value,
                verification_command=tenant_policy.test_cmd,
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
        if (
            result.inspection_snapshot is not None
            or result.dependency_slice is not None
        ):
            await store.set_inspection_evidence(
                pool,
                changeset_id,
                snapshot=result.inspection_snapshot,
                dependency_slice=result.dependency_slice,
            )
        if (
            result.verification_plan is not None
            or result.verification_coverage is not None
        ):
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
                pool,
                changeset_id,
                ChangesetStatus.error,
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
        # before returning its candidate. This re-check (on the possibly capped
        # diff_text) guards editors that do not gate themselves (for example, a
        # fake or custom Editor).
        verified_exemptions: tuple[VerifiedProtectedPathExemption, ...] = ()
        if workflow_attestation_is_valid(
            result.generated_runtime_workflow,
            plan=result.runtime_acceptance_plan,
            policy=runtime_policy,
        ):
            verified_exemptions = (
                VerifiedProtectedPathExemption(
                    content_sha256=result.generated_runtime_workflow.content_sha256,
                    runtime_acceptance_plan_sha256=(
                        result.generated_runtime_workflow.runtime_acceptance_plan_sha256
                    ),
                ),
            )
        gate = evaluate_pre_push(
            diff_stat=result.diff_stat,
            changed_paths=result.changed_paths,
            diff_text=result.diff_text,
            policy=effective_safety_policy,
            verified_exemptions=verified_exemptions,
        )
        if not gate.passed:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error="Pre-push gate failed: " + "; ".join(gate.violations),
            )
            return

        if not (
            result.head_sha
            and result.base_sha
            and result.candidate_tree_sha
            and result.patch_base64
        ):
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error=(
                    "Editor returned no complete base/tree-bound publication candidate."
                ),
            )
            return
        if result.branch is not None and result.branch != branch:
            await store.transition_changeset(
                pool,
                changeset_id,
                ChangesetStatus.error,
                error=(
                    "Editor returned a branch identity that differs from the "
                    "controller-owned canonical branch."
                ),
            )
            return

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
            result.runtime_acceptance_plan and result.runtime_acceptance_plan.blockers
        )
        draft = (
            not authorization.decision.ready_for_review
            or lacks_external_ci
            or semantic_unverified
            or runtime_unverified
        )
        pr_body = _pr_body(
            changeset.task,
            result.requirement_ledger,
            result.verification_plan,
            result.verification_coverage,
            result.runtime_acceptance_plan,
            result.review_verdict,
            authorization,
        )
        external_ci_status = (
            ExternalCIStatus.unverified_external_ci
            if lacks_external_ci
            else ExternalCIStatus.pending
        )
        await store.transition_changeset(pool, changeset_id, ChangesetStatus.pushing)
        intent = PublicationIntentRecorded(
            event_id=f"cpub_{uuid.uuid4().hex}",
            changeset_id=changeset_id,
            recorded_at=datetime.now(timezone.utc),
            repository=connection.repository_full_name,
            repository_id=connection.repository_id,
            installation_id=connection.target.installation_id,
            branch=branch,
            base_branch=base_branch,
            candidate_base_sha=result.base_sha,
            candidate_head_sha=result.head_sha,
            candidate_tree_sha=result.candidate_tree_sha,
            patch_base64=result.patch_base64,
            commit_title=changeset.task.title,
            pull_request_title=changeset.task.title,
            pull_request_body=pr_body,
            draft=draft,
            external_ci_status=external_ci_status,
            diff_stat=result.diff_stat,
        )
        # From this point the intent write may have committed even if the
        # caller observes a connection error. Never let the generic job error
        # path strand a possibly accepted GitHub mutation outside recovery.
        publication_recovery_owned = True
        await publication_store.record_intent(pool, intent)
        await resume_pull_request_publication(
            pool,
            changeset_id,
            mint_read_token=mint_read_token,
            mint_write_token=mint_write_token,
            mint_pr_write_token=mint_pr_write_token,
            branch_publisher=branch_publisher,
            open_pr=open_pr,
            find_pr=find_pr,
            close_pr=close_pr,
        )
    except Exception as exc:
        logger.exception("Changeset job %s failed", changeset_id)
        if publication_recovery_owned:
            logger.warning(
                "Changeset %s remains pushing for durable publication recovery",
                changeset_id,
            )
            return
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
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    mint_pr_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
    open_pr: PROpener,
    find_pr: PRFinder,
    close_pr: PRCloser,
) -> None:
    """Periodically fail active-state changesets that stopped moving.

    The startup sweep only catches orphans that are already old enough when the
    process boots; a changeset orphaned shortly *before* a restart would
    otherwise sit in ``cloning``/``editing``/… until some much later restart.
    This loop first resumes stale rows that have a durable publication intent,
    then fails only non-resumable active rows. A live job never trips it as long
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
                recoverable = await publication_store.list_recoverable_ids(
                    pool,
                    older_than_seconds=older_than_seconds,
                )
                for changeset_id in recoverable:
                    await resume_pull_request_publication(
                        pool,
                        changeset_id,
                        mint_read_token=mint_read_token,
                        mint_write_token=mint_write_token,
                        mint_pr_write_token=mint_pr_write_token,
                        branch_publisher=branch_publisher,
                        open_pr=open_pr,
                        find_pr=find_pr,
                        close_pr=close_pr,
                    )
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
