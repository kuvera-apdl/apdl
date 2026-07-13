"""Credential-free evaluator adapter for the production Aider candidate.

The executable reads one strict :class:`PublicEvaluationInvocation` from stdin,
runs the same ``AiderEditor`` pipeline used for a real changeset directly in the
current materialized git workspace, and writes one strict
``evaluation_execution@2`` object to stdout.  It never receives or fabricates
GitHub, publication, sealed-oracle, or post-merge evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import sys
import time
from pathlib import Path
from typing import Protocol

from app.contracts.models import ContractBundle
from app.editor.aider_editor import AiderEditor
from app.editor.base import EditRequest, EditResult
from app.evaluations.json_io import parse_strict_json_object
from app.evaluations.models import (
    DetectionChannel,
    DetectionObservation,
    EvaluationExecution,
    EvidenceReference,
    EvidenceSource,
    ExecutionMeasurements,
    MetricValue,
    ReviewerObservation,
    canonical_sha256,
)
from app.evaluations.subprocess_executor import PublicEvaluationInvocation
from app.requirements.models import ImplementationStatus, RequirementLedger
from app.semantic_review.models import ReviewDecision, ReviewVerdict

_MAX_INVOCATION_BYTES = 64 * 1024
_EVALUATION_REPOSITORY = "evaluation/candidate"
_EVALUATION_PROJECT_SCOPE = "evaluation"


class WorkspaceEditor(Protocol):
    """Narrow injection seam for deterministic candidate adapter tests."""

    async def implement_workspace(
        self,
        request: EditRequest,
        workspace: Path,
    ) -> EditResult: ...


def _unavailable(reason: str) -> MetricValue:
    return MetricValue(value=None, unavailable_reason=reason)


def _evidence(
    source: EvidenceSource,
    reference: str,
    payload: dict | list,
) -> EvidenceReference:
    return EvidenceReference(
        source=source,
        reference=reference,
        sha256=canonical_sha256(payload),
    )


def _artifact_sha256(value) -> str | None:
    if value is None:
        return None
    return canonical_sha256(value)


def _result_identity(result: EditResult) -> dict:
    """Bind executor evidence to the exact gated result and resulting diff."""
    return {
        "success": result.success,
        "branch": result.branch,
        "head_sha": result.head_sha,
        "diff_stat": result.diff_stat,
        "changed_paths": sorted(result.changed_paths),
        "diff_sha256": hashlib.sha256(result.diff_text.encode("utf-8")).hexdigest(),
        "requirement_ledger_sha256": _artifact_sha256(result.requirement_ledger),
        "contract_bundle_sha256": _artifact_sha256(result.contract_bundle),
        "review_verdict_sha256": _artifact_sha256(result.review_verdict),
    }


def _requirement_coverage(
    ledger: RequirementLedger | None,
    *,
    result_identity: dict,
) -> MetricValue:
    if ledger is None:
        return _unavailable("the candidate did not produce a requirement ledger")
    active = [
        requirement
        for requirement in ledger.requirements
        if requirement.implementation_status
        not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
    ]
    if not active:
        return _unavailable("the requirement ledger contained no active requirements")
    covered = [
        requirement
        for requirement in active
        if requirement.implementation_status
        in {
            ImplementationStatus.implemented,
            ImplementationStatus.confirmed_existing,
        }
    ]
    evidence = _evidence(
        EvidenceSource.executor,
        "candidate://requirement-ledger",
        {
            "ledger": ledger.model_dump(mode="json"),
            "result": result_identity,
        },
    )
    return MetricValue(value=len(covered) / len(active), evidence=[evidence])


def _retry_measurement(
    result: EditResult,
    *,
    result_identity: dict,
) -> MetricValue:
    edit_attempts = sum(
        1 for prompt in result.prompts if prompt.get("stage") == "edit"
    )
    retries = max(0, edit_attempts - 1)
    evidence = _evidence(
        EvidenceSource.executor,
        "candidate://edit-attempts",
        {
            "prompt_stages": [prompt.get("stage") for prompt in result.prompts],
            "edit_attempts": edit_attempts,
            "result": result_identity,
        },
    )
    return MetricValue(value=float(retries), evidence=[evidence])


def _latency_measurement(
    elapsed_seconds: float,
    *,
    invocation_id: str,
    result_identity: dict,
) -> MetricValue:
    evidence = _evidence(
        EvidenceSource.executor,
        "candidate://wall-clock",
        {
            "invocation_id": invocation_id,
            "elapsed_seconds": elapsed_seconds,
            "result": result_identity,
        },
    )
    return MetricValue(value=elapsed_seconds, evidence=[evidence])


def _contract_detection(bundle: ContractBundle | None) -> DetectionObservation | None:
    if bundle is None:
        return None
    blocked = [
        resolution
        for resolution in bundle.resolutions
        if resolution.disposition == "blocked"
    ]
    if not blocked:
        return None
    evidence = _evidence(
        EvidenceSource.contract_resolver,
        "candidate://contract-resolver/blocked",
        [resolution.model_dump(mode="json") for resolution in blocked],
    )
    return DetectionObservation(
        channel=DetectionChannel.contract_resolver,
        evidence=evidence,
    )


def _semantic_detection(
    verdict: ReviewVerdict | None,
) -> DetectionObservation | None:
    if verdict is None or verdict.overall_decision is not ReviewDecision.rejected:
        return None
    evidence = _evidence(
        EvidenceSource.semantic_review,
        "candidate://semantic-review/rejected",
        verdict.model_dump(mode="json"),
    )
    return DetectionObservation(
        channel=DetectionChannel.semantic_review,
        evidence=evidence,
    )


def _reviewer_observation(
    verdict: ReviewVerdict | None,
) -> ReviewerObservation | None:
    if verdict is None or verdict.overall_decision is ReviewDecision.unverified:
        return None
    evidence = _evidence(
        EvidenceSource.semantic_review,
        "candidate://semantic-review/final",
        verdict.model_dump(mode="json"),
    )
    return ReviewerObservation(
        predicted_defect=verdict.overall_decision is ReviewDecision.rejected,
        evidence=evidence,
    )


def _measurements(
    result: EditResult,
    *,
    invocation_id: str,
    elapsed_seconds: float,
) -> ExecutionMeasurements:
    result_identity = _result_identity(result)
    return ExecutionMeasurements(
        requirement_coverage=_requirement_coverage(
            result.requirement_ledger,
            result_identity=result_identity,
        ),
        # Production deliberately delegates repository commands and their
        # authority to GitHub.  This credential-free offline candidate does not
        # relabel absent GitHub observations as local success.
        build_success=_unavailable(
            "offline candidate execution does not run authoritative build commands"
        ),
        lint_success=_unavailable(
            "offline candidate execution does not run authoritative lint commands"
        ),
        test_success=_unavailable(
            "offline candidate execution does not run authoritative test commands"
        ),
        first_pass_ci_success=_unavailable(
            "offline candidate execution has no GitHub CI observation"
        ),
        ci_repair_success=_unavailable(
            "offline candidate execution does not perform a GitHub CI repair"
        ),
        failure_classification_correct=_unavailable(
            "no oracle-labeled CI failure was presented to the candidate"
        ),
        reverted=_unavailable(
            "offline candidate execution has no post-merge revert observation"
        ),
        human_correction_lines=_unavailable(
            "offline candidate execution has no human correction observation"
        ),
        retries=_retry_measurement(result, result_identity=result_identity),
        latency_seconds=_latency_measurement(
            elapsed_seconds,
            invocation_id=invocation_id,
            result_identity=result_identity,
        ),
        cost_usd=_unavailable(
            "the production editor does not expose reliable provider cost evidence"
        ),
    )


async def evaluate_candidate(
    invocation: PublicEvaluationInvocation,
    *,
    workspace: Path,
    editor: WorkspaceEditor | None = None,
) -> EvaluationExecution:
    """Run one public invocation without access to evaluator-only identity."""
    resolved_editor = editor or AiderEditor()
    request = EditRequest(
        repo=_EVALUATION_REPOSITORY,
        project_scope=_EVALUATION_PROJECT_SCOPE,
        base_branch="main",
        branch=f"evaluation/{invocation.invocation_id}",
        token="",
        title=invocation.task.title,
        spec=invocation.task.spec,
        constraints=list(invocation.task.constraints),
        risk_level=invocation.task.risk.value,
    )
    started = time.monotonic()
    result = await resolved_editor.implement_workspace(request, workspace)
    elapsed = time.monotonic() - started

    detections = [
        detection
        for detection in (
            _contract_detection(result.contract_bundle),
            _semantic_detection(result.review_verdict),
        )
        if detection is not None
    ]
    notes = [
        (
            "candidate pipeline completed without publication"
            if result.success
            else "candidate pipeline did not produce a publishable result"
        )
    ]
    return EvaluationExecution(
        invocation_id=invocation.invocation_id,
        detections=detections,
        reviewer=_reviewer_observation(result.review_verdict),
        measurements=_measurements(
            result,
            invocation_id=invocation.invocation_id,
            elapsed_seconds=elapsed,
        ),
        notes=notes,
    )


def _read_invocation() -> PublicEvaluationInvocation:
    raw = sys.stdin.buffer.read(_MAX_INVOCATION_BYTES + 1)
    if len(raw) > _MAX_INVOCATION_BYTES:
        raise ValueError("evaluation invocation exceeded its input limit")
    decoded = raw.decode("utf-8", errors="strict")
    parse_strict_json_object(decoded)
    return PublicEvaluationInvocation.model_validate_json(decoded)


def main() -> None:
    # Shared production components may log raw provider/repository exception
    # text. Evaluator stderr is a trust boundary, so suppress Python logging in
    # this single-purpose process and emit only the fixed messages below.
    logging.disable(logging.CRITICAL)
    try:
        invocation = _read_invocation()
    except (UnicodeDecodeError, ValueError):
        # Input and validation errors may contain candidate-controlled text.  Do
        # not reflect them to stderr; the controller receives a stable failure.
        print("evaluation candidate rejected an invalid invocation", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        # Third-party model libraries occasionally print diagnostics. Keep the
        # stdout protocol a single JSON object even when they do.
        with contextlib.redirect_stdout(sys.stderr):
            execution = asyncio.run(
                evaluate_candidate(invocation, workspace=Path.cwd())
            )
    except Exception:
        # ``AiderEditor`` already converts ordinary attempt failures to an
        # EditResult.  Anything reaching here is an infrastructure fault, and
        # its raw text is deliberately withheld from the executor boundary.
        print("evaluation candidate failed with an internal error", file=sys.stderr)
        raise SystemExit(1) from None

    sys.stdout.write(execution.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
