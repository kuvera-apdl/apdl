"""Bounded, immutable, exact-head remediation of actionable GitHub CI failures."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import asyncpg

from app.config import codegen_ci_repair_budget_seconds, codegen_ci_repair_retries
from app.editor.base import Editor, EditRequest, EditResult
from app.models.observations import (
    CIRemediationAttempt,
    CIRemediationStatus,
    CIVerificationObservation,
    FailureClassification,
    RemediationPromptEvidence,
    RemediationDisposition,
)
from app.safety.gates import evaluate_pre_push
from app.store import changesets as changeset_store
from app.store import connections as connections_store
from app.store.observations import (
    claim_failed_ci_observation,
    insert_ci_remediation_attempt,
    project_repair_result,
    set_remediation_in_progress,
)

logger = logging.getLogger(__name__)
TokenMinter = Callable[[int, str], Awaitable[str]]


def _classify_failure(
    observation: CIVerificationObservation,
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
    if any(
        signal.annotations
        or any(
            term in f"{signal.name} {signal.summary or ''}".lower()
            for term in ("test", "assert", "lint", "typecheck", "build", "compile")
        )
        for signal in observation.signals
    ):
        return FailureClassification.actionable_code, 0.85
    return FailureClassification.unknown, 0.4


def _repair_spec(
    original_spec: str,
    observation: CIVerificationObservation,
    attempt: int,
    maximum: int,
) -> str:
    return (
        f"{original_spec}\n\n"
        f"GitHub CI repair attempt {attempt} of {maximum} for exact failed head "
        f"`{observation.head_sha}`. Diagnose and fix only the evidence below on "
        "the existing pull-request branch. Preserve the original intent and do "
        "not suppress, skip, or weaken checks.\n\n"
        f"GitHub CI observation `{observation.observation_id}`:\n"
        f"{observation.failure_summary or 'GitHub CI failed.'}"
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
        await changeset_store.set_contract_bundle(pool, changeset_id, result.contract_bundle)
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
        ),
    )


async def repair_failed_ci(
    pool: asyncpg.Pool,
    observation: CIVerificationObservation,
    *,
    editor: Editor,
    mint_token: TokenMinter,
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
    classification, confidence = _classify_failure(observation)
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
        )
        return

    await set_remediation_in_progress(
        pool,
        changeset_id=observation.changeset_id,
        failed_head_sha=observation.head_sha,
        status=CIRemediationStatus.repairing,
    )
    changeset = await changeset_store.get_changeset(pool, observation.changeset_id)
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
        )
        return
    connection = await connections_store.get_connection(pool, changeset.project_id)
    if connection is None:
        await _finish_without_repair(
            pool,
            observation=observation,
            attempt_id=attempt_id,
            attempt_number=claim.attempt_number,
            classification=classification,
            confidence=confidence,
            started_at=started_at,
            error="Cannot repair CI because the repository connection is unavailable.",
            exhausted=True,
        )
        return
    policy = connection.policy if isinstance(connection.policy, dict) else {}
    try:
        token = await mint_token(connection.installation_id, connection.repo)
        result = await editor.implement(
            EditRequest(
                repo=connection.repo,
                project_scope=changeset.project_id,
                requirement_ledger=changeset.requirement_ledger,
                inspection_snapshot=changeset.inspection_snapshot,
                dependency_slice=changeset.dependency_slice,
                verification_plan=changeset.verification_plan,
                verification_coverage=changeset.verification_coverage,
                base_branch=changeset.base_branch or connection.default_base_branch,
                branch=changeset.branch,
                token=token,
                title=f"Repair CI: {changeset.task.title}",
                spec=_repair_spec(
                    changeset.task.spec,
                    observation,
                    claim.attempt_number,
                    maximum,
                ),
                constraints=changeset.task.constraints,
                test_cmd=policy.get("test_cmd"),
                gates_policy=policy.get("gates"),
                existing_branch=True,
                expected_head_sha=observation.head_sha,
                risk_level=str(
                    changeset.task.context.get("risk_level") or "low"
                ).lower(),
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
        )
        return
    if result.requirement_ledger is None:
        result.requirement_ledger = changeset.requirement_ledger
    await _persist_result_evidence(
        pool, observation.changeset_id, result, changeset.prompts
    )
    gate = evaluate_pre_push(
        diff_stat=result.diff_stat,
        changed_paths=result.changed_paths,
        diff_text=result.diff_text,
        policy=policy.get("gates"),
    )
    success = result.success and gate.passed and bool(result.head_sha)
    error = result.error
    if result.success and not gate.passed:
        error = "CI repair pre-push gate failed: " + "; ".join(gate.violations)
    if result.success and not result.head_sha:
        error = "CI repair did not return the pushed head SHA."
    projected = await project_repair_result(
        pool,
        changeset_id=observation.changeset_id,
        failed_head_sha=observation.head_sha,
        resulting_head_sha=result.head_sha if success else None,
        exhausted=not success,
        error=error,
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
                None if disposition is RemediationDisposition.awaiting_ci else finished_at
            ),
            result=result,
            error=error,
        ),
    )
    logger.info(
        "CI remediation %s for changeset %s head %s",
        disposition.value,
        observation.changeset_id,
        observation.head_sha,
    )
