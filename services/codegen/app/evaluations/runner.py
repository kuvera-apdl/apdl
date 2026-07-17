"""Fixture-backed evaluator with a sealed oracle and explicit publish boundary."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from app.evaluations.corpus import (
    DEFAULT_FIXTURE_ROOT,
    load_oracle_set,
    validate_corpus_oracles,
)
from app.evaluations.fixtures import materialize_fixture, run_fixture_harness
from app.evaluations.models import (
    Ecosystem,
    EvaluationCorpus,
    EvaluationExecution,
    EvaluationOracle,
    EvaluationOutcome,
    EvaluationTask,
    EvidenceReference,
    EvidenceSource,
    MetricValue,
    OutcomeMeasurements,
    OutcomeStatus,
    RolloutStage,
    canonical_sha256,
)


@dataclass(frozen=True)
class EvaluationInvocation:
    """Only public task and workspace data cross the executor boundary."""

    invocation_id: str
    ecosystem: Ecosystem
    task: EvaluationTask
    workspace: Path


class EvaluationExecutor(Protocol):
    async def execute(self, invocation: EvaluationInvocation) -> EvaluationExecution: ...


def _score_execution(
    execution: EvaluationExecution,
    *,
    case_id: str,
    harness,
    oracle: EvaluationOracle,
    egress_attestation_sha256: str | None,
) -> EvaluationOutcome:
    recognized = [
        detection
        for detection in execution.detections
        if detection.channel in oracle.expected_detection
    ]
    unexpected = [
        detection.channel.value
        for detection in execution.detections
        if detection.channel not in oracle.expected_detection
    ]
    notes = list(execution.notes)
    if unexpected:
        notes.append(
            "unrecognized detection channels were excluded from scoring: "
            + ", ".join(sorted(unexpected))
        )
    if harness.passed:
        status = OutcomeStatus.accepted
        scored_detections = recognized
    elif recognized:
        status = OutcomeStatus.detected
        scored_detections = recognized
    else:
        status = OutcomeStatus.escaped
        scored_detections = []

    harness_evidence = EvidenceReference(
        source=EvidenceSource.fixture_harness,
        reference=f"fixture://{harness.fixture_id}/harness",
        sha256=harness.evidence_sha256,
    )
    measurements = OutcomeMeasurements.model_validate(
        {
            **execution.measurements.model_dump(mode="python"),
            "behavioral_acceptance": MetricValue(
                value=float(harness.passed),
                evidence=[harness_evidence],
            ).model_dump(mode="python"),
        }
    )
    return EvaluationOutcome(
        case_id=case_id,
        status=status,
        detections=scored_detections,
        reviewer=execution.reviewer,
        measurements=measurements,
        harness=harness,
        oracle_case_sha256=canonical_sha256(oracle),
        egress_attestation_sha256=egress_attestation_sha256,
        notes=notes,
    )


def _unavailable_outcome(
    *,
    case_id: str,
    oracle: EvaluationOracle,
    reason: str,
    egress_attestation_sha256: str | None,
) -> EvaluationOutcome:
    unavailable = lambda metric: MetricValue(  # noqa: E731 - compact strict record
        value=None,
        unavailable_reason=f"{metric} unavailable because {reason}",
    )
    return EvaluationOutcome(
        case_id=case_id,
        status=OutcomeStatus.unavailable,
        measurements=OutcomeMeasurements(
            requirement_coverage=unavailable("requirement coverage"),
            build_success=unavailable("build result"),
            lint_success=unavailable("lint result"),
            test_success=unavailable("test result"),
            first_pass_ci_success=unavailable("first-pass CI result"),
            ci_repair_success=unavailable("CI repair result"),
            failure_classification_correct=unavailable("failure classification"),
            reverted=unavailable("revert observation"),
            human_correction_lines=unavailable("human correction observation"),
            retries=unavailable("retry count"),
            latency_seconds=unavailable("latency"),
            cost_usd=unavailable("cost"),
            behavioral_acceptance=unavailable("sealed harness result"),
        ),
        harness=None,
        oracle_case_sha256=canonical_sha256(oracle),
        egress_attestation_sha256=egress_attestation_sha256,
        unavailable_reason=reason,
    )


def _trusted_egress_attestation_sha256(
    executor: EvaluationExecutor,
    invocation_id: str,
) -> str | None:
    provider = getattr(executor, "egress_attestation_sha256", None)
    if provider is None:
        return None
    value = provider(invocation_id)
    if value is not None and not isinstance(value, str):
        raise TypeError("executor egress attestation digest must be a string")
    return value


async def run_corpus(
    corpus: EvaluationCorpus,
    *,
    stage: RolloutStage,
    executor: EvaluationExecutor,
    fixture_root: Path = DEFAULT_FIXTURE_ROOT,
) -> list[EvaluationOutcome]:
    """Run real mutated fixtures in offline or non-publishing shadow mode only."""
    if stage not in {RolloutStage.offline, RolloutStage.shadow}:
        raise ValueError("evaluation corpus execution is restricted to offline and shadow")
    oracle_set = load_oracle_set()
    validate_corpus_oracles(corpus, oracle_set)
    oracle_by_case = {oracle.case_id: oracle for oracle in oracle_set.oracles}

    configured_root = os.getenv("APDL_EVALUATION_WORKSPACE_ROOT", "").strip()
    workspace_root = Path(configured_root).resolve() if configured_root else None
    if workspace_root is not None:
        if not workspace_root.is_absolute() or not workspace_root.is_dir():
            raise ValueError(
                "APDL_EVALUATION_WORKSPACE_ROOT must name an existing absolute directory"
            )

    outcomes: list[EvaluationOutcome] = []
    for case in corpus.cases:
        invocation_id = f"eval_inv_{secrets.token_hex(16)}"
        with TemporaryDirectory(
            prefix="apdl-eval-workspace-",
            dir=workspace_root,
        ) as temp_dir:
            materialized, manifest = materialize_fixture(
                case,
                Path(temp_dir) / "checkout",
                fixture_root=fixture_root,
            )
            try:
                execution = await executor.execute(
                    EvaluationInvocation(
                        invocation_id=invocation_id,
                        ecosystem=case.ecosystem,
                        task=case.task,
                        workspace=materialized.workspace,
                    )
                )
            except Exception as exc:
                # A single model/provider/container failure must not discard the
                # other cases.  It becomes explicit denominator/exclusion data;
                # no candidate-controlled exception text enters the artifact.
                outcomes.append(
                    _unavailable_outcome(
                        case_id=case.case_id,
                        oracle=oracle_by_case[case.case_id],
                        reason=f"evaluation executor failed ({type(exc).__name__})",
                        egress_attestation_sha256=(
                            _trusted_egress_attestation_sha256(
                                executor,
                                invocation_id,
                            )
                        ),
                    )
                )
                continue
            if execution.invocation_id != invocation_id:
                raise ValueError("executor returned a result for a different invocation")
            harness = run_fixture_harness(materialized, manifest)
            outcomes.append(
                _score_execution(
                    execution,
                    case_id=case.case_id,
                    harness=harness,
                    oracle=oracle_by_case[case.case_id],
                    egress_attestation_sha256=(
                        _trusted_egress_attestation_sha256(
                            executor,
                            invocation_id,
                        )
                    ),
                )
            )
    return outcomes
