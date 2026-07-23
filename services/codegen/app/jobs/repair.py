"""Bounded, immutable, exact-head remediation of actionable GitHub CI failures."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

import asyncpg

from app.config import codegen_ci_repair_budget_seconds, codegen_ci_repair_retries
from app.editor.base import Editor, EditRequest, EditResult
from app.github.publisher import BranchPublisher
from app.models.observations import (
    CIRemediationAttempt,
    CIRemediationStatus,
    CIVerificationObservation,
    FailureClassification,
    RemediationPromptEvidence,
    RemediationDisposition,
)
from app.publication import PublicationGate
from app.runtime.github_actions import workflow_attestation_is_valid
from app.runtime.models import RuntimeAcceptancePolicy, RuntimeEvidenceObservation
from app.safety.gates import evaluate_pre_push
from app.safety.policy import (
    PlatformCodegenSafetyPolicy,
    VerifiedProtectedPathExemption,
    resolve_effective_policy,
)
from app.safety.secrets import redact_secrets
from app.store import changesets as changeset_store
from app.store import connections as connections_store
from app.store.observations import (
    claim_failed_ci_observation,
    insert_ci_remediation_attempt,
    project_repair_result,
    set_remediation_in_progress,
)
from app.store.runtime_evidence import latest_runtime_evidence_observation

logger = logging.getLogger(__name__)
TokenMinter = Callable[[str], AbstractAsyncContextManager[str]]

_RUNTIME_JOB_LOG_LIMIT = 3
_RUNTIME_JOB_LOG_BYTES = 2_000
_RUNTIME_ARTIFACT_LIMIT = 4
_RUNTIME_ARTIFACT_FILE_LIMIT = 2
_RUNTIME_ARTIFACT_EXCERPT_BYTES = 1_000
_RUNTIME_DIAGNOSTIC_LIMIT = 5
_RUNTIME_DIAGNOSTIC_BYTES = 800


def _bounded_utf8(value: str, limit: int) -> str:
    """Retain at most ``limit`` UTF-8 bytes without splitting a character."""
    value = redact_secrets(value)[0]
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = "\n...[bounded by APDL]"
    available = max(0, limit - len(suffix.encode("utf-8")))
    prefix = encoded[:available].decode("utf-8", errors="ignore")
    return prefix + suffix


def _render_runtime_evidence(
    observation: RuntimeEvidenceObservation | None,
) -> str:
    """Render bounded, immutable GitHub runtime evidence for one repair prompt."""
    if observation is None:
        return "No exact-head GitHub runtime evidence observation was available."

    values = [
        f"Runtime evidence observation `{observation.observation_id}` ",
        f"(SHA-256 `{observation.evidence_hash()}`) for exact head ",
        f"`{observation.head_sha}`.",
    ]
    logs = observation.job_logs[:_RUNTIME_JOB_LOG_LIMIT]
    values.append("\n\nGitHub Actions job-log excerpts:")
    if not logs:
        values.append("\n- None collected.")
    for log in logs:
        values.extend(
            (
                f"\n- Run {log.workflow_run_id}, job {log.job_id} ",
                f"`{log.job_name}` ({log.github_url}); ",
                f"source_bytes={log.source_byte_count}, ",
                f"collector_truncated={str(log.truncated).lower()}, ",
                f"collector_redacted={str(log.redacted).lower()}:\n",
                _bounded_utf8(log.text_excerpt, _RUNTIME_JOB_LOG_BYTES),
            )
        )
    if len(observation.job_logs) > len(logs):
        values.append(
            f"\n- {len(observation.job_logs) - len(logs)} additional job log(s) "
            "omitted by the repair prompt bound."
        )

    artifacts = observation.artifacts[:_RUNTIME_ARTIFACT_LIMIT]
    values.append("\n\nGitHub Actions artifact excerpts:")
    if not artifacts:
        values.append("\n- None collected.")
    for artifact in artifacts:
        values.append(
            f"\n- Run {artifact.workflow_run_id}, artifact "
            f"`{artifact.artifact_name}`; status={artifact.status.value}; "
            f"requirements={','.join(artifact.requirement_ids)}."
        )
        if artifact.unverified_reason:
            values.append(
                " Reason: "
                + _bounded_utf8(
                    artifact.unverified_reason, _RUNTIME_ARTIFACT_EXCERPT_BYTES
                )
            )
        files = artifact.files[:_RUNTIME_ARTIFACT_FILE_LIMIT]
        for file in files:
            values.append(
                f"\n  - `{file.path}` sha256={file.content_sha256} "
                f"bytes={file.byte_count}:\n"
            )
            values.append(
                _bounded_utf8(
                    file.text_excerpt or "(binary artifact; no text excerpt)",
                    _RUNTIME_ARTIFACT_EXCERPT_BYTES,
                )
            )
        if len(artifact.files) > len(files):
            values.append(
                f"\n  - {len(artifact.files) - len(files)} additional file(s) "
                "omitted by the repair prompt bound."
            )
    if len(observation.artifacts) > len(artifacts):
        values.append(
            f"\n- {len(observation.artifacts) - len(artifacts)} additional artifact(s) "
            "omitted by the repair prompt bound."
        )

    diagnostics = observation.collection_errors[:_RUNTIME_DIAGNOSTIC_LIMIT]
    values.append("\n\nRuntime evidence collection diagnostics:")
    if not diagnostics:
        values.append("\n- None.")
    for diagnostic in diagnostics:
        values.append("\n- " + _bounded_utf8(diagnostic, _RUNTIME_DIAGNOSTIC_BYTES))
    if len(observation.collection_errors) > len(diagnostics):
        values.append(
            f"\n- {len(observation.collection_errors) - len(diagnostics)} additional "
            "diagnostic(s) omitted by the repair prompt bound."
        )
    # Reapply the canonical policy to the complete rendering as a final boundary.
    # Metadata fields (job names, artifact paths, and URLs) are untrusted too,
    # even when their excerpts were already redacted at collection time.
    return redact_secrets("".join(values))[0]


def _classify_failure(
    observation: CIVerificationObservation,
    runtime_evidence: RuntimeEvidenceObservation | None = None,
) -> tuple[FailureClassification, float]:
    text = " ".join(
        [
            observation.failure_summary or "",
            *(signal.name for signal in observation.signals),
            *(signal.summary or "" for signal in observation.signals),
        ]
    ).lower()
    if any(term in text for term in ("branch protection", "required review", "policy")):
        return FailureClassification.policy, 0.9
    if any(
        term in text
        for term in (
            "runner unavailable",
            "service unavailable",
            "network error",
            "rate limit",
            "infrastructure",
        )
    ):
        return FailureClassification.infrastructure, 0.85
    if any(term in text for term in ("flaky", "intermittent", "retryable timeout")):
        return FailureClassification.flaky, 0.8
    if any(signal.annotations for signal in observation.signals) or any(
        term in text
        for term in ("test", "assert", "lint", "typecheck", "build", "compile")
    ):
        return FailureClassification.actionable_code, 0.85
    if runtime_evidence is not None and runtime_evidence.job_logs:
        # The collector retains logs only for structurally failed Actions jobs.
        # Arbitrary log text is repair context, never keyword-classifier input.
        return FailureClassification.actionable_code, 0.65
    return FailureClassification.unknown, 0.4


def _repair_spec(
    original_spec: str,
    observation: CIVerificationObservation,
    attempt: int,
    maximum: int,
    runtime_evidence: RuntimeEvidenceObservation | None = None,
) -> str:
    return (
        f"{original_spec}\n\n"
        f"GitHub CI repair attempt {attempt} of {maximum} for exact failed head "
        f"`{observation.head_sha}`. Diagnose and fix only the evidence below on "
        "the existing pull-request branch. Preserve the original intent and do "
        "not suppress, skip, or weaken checks.\n\n"
        f"GitHub CI observation `{observation.observation_id}`:\n"
        f"{observation.failure_summary or 'GitHub CI failed.'}\n\n"
        f"{_render_runtime_evidence(runtime_evidence)}"
    )


def _prompt_evidence(result: EditResult) -> list[RemediationPromptEvidence]:
    values: list[RemediationPromptEvidence] = []
    for prompt in result.prompts:
        content = str(prompt.get("user") or "")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        values.append(
            RemediationPromptEvidence(
                evidence_id=f"prompt:{sha[:24]}",
                content_sha256=sha,
                stage=str(prompt.get("stage") or "unknown"),
                label=str(prompt.get("label") or "prompt"),
                excerpt=content[:2000] or "(empty prompt)",
            )
        )
    return list({item.evidence_id: item for item in values}.values())


def _attempt_event(
    *,
    attempt_id: str,
    sequence: int,
    observation: CIVerificationObservation,
    attempt_number: int,
    classification: FailureClassification,
    confidence: float,
    disposition: RemediationDisposition,
    started_at: datetime,
    recorded_at: datetime,
    finished_at: datetime | None = None,
    result: EditResult | None = None,
    error: str | None = None,
    runtime_evidence: RuntimeEvidenceObservation | None = None,
) -> CIRemediationAttempt:
    prompt_evidence = _prompt_evidence(result) if result else []
    return CIRemediationAttempt(
        attempt_id=attempt_id,
        event_sequence=sequence,
        event_id=f"{attempt_id}:{sequence}",
        changeset_id=observation.changeset_id,
        repository=observation.repository,
        pr_number=observation.pr_number,
        failed_head_sha=observation.head_sha,
        failure_observation_id=observation.observation_id,
        attempt_number=attempt_number,
        classification=classification,
        confidence=confidence,
        runtime_evidence_observation_id=(
            runtime_evidence.observation_id if runtime_evidence else None
        ),
        runtime_evidence_hash=(
            runtime_evidence.evidence_hash() if runtime_evidence else None
        ),
        prompt_evidence_ids=[item.evidence_id for item in prompt_evidence],
        prompt_evidence=prompt_evidence,
        changed_files=sorted(set(result.changed_paths)) if result else [],
        resulting_commit_sha=result.head_sha if result else None,
        disposition=disposition,
        started_at=started_at,
        recorded_at=recorded_at,
        finished_at=finished_at,
        error=error,
    )


async def _persist_result_evidence(
    pool: asyncpg.Pool, changeset_id: str, result: EditResult, prior_prompts: list[dict]
) -> None:
    if result.contract_bundle is not None:
        await changeset_store.set_contract_bundle(
            pool, changeset_id, result.contract_bundle
        )
    if result.requirement_ledger is not None:
        await changeset_store.set_requirement_ledger(
            pool, changeset_id, result.requirement_ledger
        )
    if result.inspection_snapshot is not None or result.dependency_slice is not None:
        await changeset_store.set_inspection_evidence(
            pool,
            changeset_id,
            snapshot=result.inspection_snapshot,
            dependency_slice=result.dependency_slice,
        )
    if result.verification_plan is not None or result.verification_coverage is not None:
        await changeset_store.set_verification_evidence(
            pool,
            changeset_id,
            plan=result.verification_plan,
            coverage=result.verification_coverage,
        )
    if result.review_verdict is not None:
        await changeset_store.set_review_verdict(
            pool, changeset_id, result.review_verdict
        )
    if result.prompts:
        await changeset_store.set_prompts(
            pool, changeset_id, [*prior_prompts, *result.prompts]
        )


async def _finish_without_repair(
    pool: asyncpg.Pool,
    *,
    observation: CIVerificationObservation,
    attempt_id: str,
    attempt_number: int,
    classification: FailureClassification,
    confidence: float,
    started_at: datetime,
    error: str,
    exhausted: bool,
    disposition: RemediationDisposition | None = None,
    runtime_evidence: RuntimeEvidenceObservation | None = None,
) -> None:
    """Close a claimed attempt without ever leaving a mutable status wedged."""
    projected = await project_repair_result(
        pool,
        changeset_id=observation.changeset_id,
        failed_head_sha=observation.head_sha,
        resulting_head_sha=None,
        exhausted=exhausted,
        error=error,
    )
    finished_at = datetime.now(timezone.utc)
    final_disposition = (
        disposition
        if projected and disposition is not None
        else RemediationDisposition.exhausted
        if projected
        else RemediationDisposition.superseded
    )
    await insert_ci_remediation_attempt(
        pool,
        _attempt_event(
            attempt_id=attempt_id,
            sequence=2,
            observation=observation,
            attempt_number=attempt_number,
            classification=classification,
            confidence=confidence,
            disposition=final_disposition,
            started_at=started_at,
            recorded_at=finished_at,
            finished_at=finished_at,
            error=error,
            runtime_evidence=runtime_evidence,
        ),
    )


async def repair_failed_ci(
    pool: asyncpg.Pool,
    observation: CIVerificationObservation,
    *,
    editor: Editor,
    mint_read_token: TokenMinter,
    mint_write_token: TokenMinter,
    branch_publisher: BranchPublisher,
    publication_gate: PublicationGate,
    platform_safety_policy: PlatformCodegenSafetyPolicy | None = None,
) -> None:
    """Repair one persisted exact-head failure or leave immutable diagnostics."""
    scopes = observation.remediation_claim_scopes()
    if not scopes:
        return
    maximum = codegen_ci_repair_retries()
    claim = await claim_failed_ci_observation(
        pool,
        observation,
        claim_scope=scopes[0],
        max_attempts=maximum,
        budget_seconds=codegen_ci_repair_budget_seconds(),
    )
    if not claim.claimed or claim.attempt_number is None:
        return

    started_at = datetime.now(timezone.utc)
    try:
        runtime_evidence = await latest_runtime_evidence_observation(
            pool,
            observation.changeset_id,
            head_sha=observation.head_sha,
            ci_observation_id=observation.observation_id,
        )
    except Exception:
        logger.warning(
            "Could not read optional runtime evidence for CI observation %s",
            observation.observation_id,
            exc_info=True,
        )
        runtime_evidence = None
    try:
        changeset = await changeset_store.get_changeset(pool, observation.changeset_id)
    except Exception:
        logger.warning(
            "Could not read changeset while preparing CI repair %s",
            observation.observation_id,
            exc_info=True,
        )
        changeset = None
    if runtime_evidence is not None and (
        runtime_evidence.repository != observation.repository
        or runtime_evidence.pr_number != observation.pr_number
        or runtime_evidence.head_sha != observation.head_sha
        or runtime_evidence.ci_observation_id != observation.observation_id
        or runtime_evidence.ci_evidence_hash != observation.evidence_hash()
        or runtime_evidence.assessment.external_ci_status is not observation.status
        or changeset is None
        or changeset.runtime_acceptance_plan is None
        or runtime_evidence.runtime_acceptance_plan_sha256
        != changeset.runtime_acceptance_plan.evidence_hash()
    ):
        logger.warning(
            "Ignoring mismatched runtime evidence %s for CI observation %s",
            runtime_evidence.observation_id,
            observation.observation_id,
        )
        runtime_evidence = None
    classification, confidence = _classify_failure(observation, runtime_evidence)
    attempt_id = (
        f"repair:{observation.changeset_id}:{observation.head_sha}:"
        f"{claim.attempt_number}"
    )
    await insert_ci_remediation_attempt(
        pool,
        _attempt_event(
            attempt_id=attempt_id,
            sequence=1,
            observation=observation,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            disposition=RemediationDisposition.diagnosing,
            started_at=started_at,
            recorded_at=started_at,
            runtime_evidence=runtime_evidence,
        ),
    )

    if classification is not FailureClassification.actionable_code:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error=(
                f"CI failure classified as {classification.value}; "
                "use GitHub-native rerun or human diagnosis."
            ),
            exhausted=False,
            disposition=RemediationDisposition.not_actionable,
            runtime_evidence=runtime_evidence,
        )
        return

    await set_remediation_in_progress(
        pool,
        changeset_id=observation.changeset_id,
        failed_head_sha=observation.head_sha,
        status=CIRemediationStatus.repairing,
    )
    if changeset is None or not changeset.branch:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error="Cannot repair CI because the changeset branch is unavailable.",
            exhausted=True,
            runtime_evidence=runtime_evidence,
        )
        return
    connection = await connections_store.get_connection_for_changeset(
        pool, observation.changeset_id
    )
    if connection is None:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error="Cannot repair CI because the repository grant is unavailable or revoked.",
            exhausted=True,
            runtime_evidence=runtime_evidence,
        )
        return
    try:
        risk = changeset.controls.risk_level
        authorization = publication_gate.authorize(
            risk=risk,
            canary_identity=f"{changeset.project_id}:{connection.repository_id}",
        )
        await changeset_store.set_publication_authorization(
            pool, observation.changeset_id, authorization
        )
    except Exception as exc:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error=(
                "CI repair publication authorization failed before GitHub "
                f"credential minting: {exc}"
            ),
            exhausted=True,
            runtime_evidence=runtime_evidence,
        )
        return
    if not authorization.decision.allowed:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error=(
                "CI repair rollout denied before GitHub credential minting: "
                + "; ".join(authorization.decision.reasons)
            ),
            exhausted=True,
            runtime_evidence=runtime_evidence,
        )
        return
    if not authorization.decision.publish_branch:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error="CI repair rollout did not grant branch publication.",
            exhausted=True,
            runtime_evidence=runtime_evidence,
        )
        return
    try:
        tenant_policy = changeset.tenant_policy_snapshot or connection.tenant_policy
        effective_safety_policy = resolve_effective_policy(
            tenant_policy,
            platform_safety_policy,
        )
        runtime_policy = RuntimeAcceptancePolicy(
            enabled=effective_safety_policy.runtime_workflow_generation_enabled
        )
        await changeset_store.set_safety_policy_provenance(
            pool,
            observation.changeset_id,
            tenant_policy_snapshot=tenant_policy,
            effective_safety_policy_sha256=(effective_safety_policy.canonical_digest()),
        )
        async with mint_read_token(observation.changeset_id) as token:
            result = await editor.implement(
                EditRequest(
                    repo=connection.repository_full_name,
                    project_scope=changeset.project_id,
                    requirement_ledger=changeset.requirement_ledger,
                    inspection_snapshot=changeset.inspection_snapshot,
                    dependency_slice=changeset.dependency_slice,
                    verification_plan=changeset.verification_plan,
                    verification_coverage=changeset.verification_coverage,
                    runtime_acceptance_plan=changeset.runtime_acceptance_plan,
                    runtime_acceptance_policy=runtime_policy,
                    base_branch=(
                        changeset.base_branch or connection.default_base_branch
                    ),
                    branch=changeset.branch,
                    token=token,
                    title=f"Repair CI: {changeset.task.title}",
                    spec=_repair_spec(
                        changeset.task.spec,
                        observation,
                        claim.attempt_number,
                        maximum,
                        runtime_evidence,
                    ),
                    constraints=changeset.task.constraints,
                    test_cmd=tenant_policy.test_cmd,
                    safety_policy=effective_safety_policy,
                    existing_branch=True,
                    expected_head_sha=observation.head_sha,
                    risk_level=risk.value,
                )
            )
    except Exception as exc:
        logger.exception(
            "CI remediation editor failed for changeset %s head %s",
            observation.changeset_id,
            observation.head_sha,
        )
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error=f"CI repair editor failed: {exc}",
            exhausted=True,
            runtime_evidence=runtime_evidence,
        )
        return
    if result.requirement_ledger is None:
        result.requirement_ledger = changeset.requirement_ledger
    await _persist_result_evidence(
        pool, observation.changeset_id, result, changeset.prompts
    )
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
    has_candidate = bool(
        result.head_sha
        and result.base_sha
        and result.candidate_tree_sha
        and result.patch_base64
    )
    success = result.success and gate.passed and has_candidate
    error = result.error
    if result.success and not gate.passed:
        error = "CI repair pre-push gate failed: " + "; ".join(gate.violations)
    if result.success and gate.passed and not has_candidate:
        error = (
            "CI repair did not return a complete base/tree-bound publication candidate."
        )
    if success:
        try:
            assert result.base_sha is not None
            assert result.candidate_tree_sha is not None
            assert result.patch_base64 is not None
            async with mint_read_token(observation.changeset_id) as token:
                async with branch_publisher.prepare(
                    repository=connection.repository_full_name,
                    branch=changeset.branch,
                    base_branch=(
                        changeset.base_branch or connection.default_base_branch
                    ),
                    expected_base_sha=result.base_sha,
                    expected_remote_sha=observation.head_sha,
                    candidate_head_sha=result.head_sha,
                    candidate_tree_sha=result.candidate_tree_sha,
                    patch_base64=result.patch_base64,
                    commit_title=f"Repair CI: {changeset.task.title}",
                    read_token=token,
                ) as prepared:
                    async with mint_write_token(
                        observation.changeset_id
                    ) as write_token:
                        published = await branch_publisher.push(
                            prepared,
                            write_token=write_token,
                        )
            result.branch = published.branch
            result.head_sha = published.head_sha
        except Exception as exc:
            logger.exception(
                "Controller-owned CI repair publication failed for %s",
                observation.changeset_id,
            )
            success = False
            error = f"CI repair publication failed: {exc}"
    projected = await project_repair_result(
        pool,
        changeset_id=observation.changeset_id,
        failed_head_sha=observation.head_sha,
        resulting_head_sha=result.head_sha if success else None,
        exhausted=not success,
        error=error,
        runtime_acceptance_plan=(result.runtime_acceptance_plan if success else None),
    )
    finished_at = datetime.now(timezone.utc)
    disposition = (
        RemediationDisposition.awaiting_ci
        if success and projected
        else RemediationDisposition.superseded
        if success
        else RemediationDisposition.exhausted
    )
    await insert_ci_remediation_attempt(
        pool,
        _attempt_event(
            attempt_id=attempt_id,
            sequence=2,
            observation=observation,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            disposition=disposition,
            started_at=started_at,
            recorded_at=finished_at,
            finished_at=(
                None
                if disposition is RemediationDisposition.awaiting_ci
                else finished_at
            ),
            result=result,
            error=error,
            runtime_evidence=runtime_evidence,
        ),
    )
    logger.info(
        "CI remediation %s for changeset %s head %s",
        disposition.value,
        observation.changeset_id,
        observation.head_sha,
    )
