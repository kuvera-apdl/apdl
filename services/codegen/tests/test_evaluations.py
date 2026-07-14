"""Continuous-evaluation fixtures, metrics, reports, and rollout controls."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

from app.evaluations.corpus import (
    DEFAULT_FIXTURE_ROOT,
    load_corpus,
    load_oracle_set,
    validate_corpus_oracles,
)
from app.evaluations.cli import MAX_EVALUATION_RUN_BYTES, load_evaluation_run, main
from app.evaluations.execution import CompletedEvaluation
from app.evaluations.fixtures import (
    fixture_sha256,
    load_fixture_manifest,
    materialize_fixture,
    run_fixture_harness,
)
from app.evaluations.metrics import aggregate_metrics, build_evaluation_report
from app.evaluations.models import (
    DetectionChannel,
    DetectionObservation,
    Ecosystem,
    EvaluationExecution,
    EvaluationOutcome,
    EvaluationReport,
    EvaluationRun,
    EvaluationSummary,
    EvaluationTask,
    EvidenceReference,
    EvidenceSource,
    ExecutionMeasurements,
    HarnessObservation,
    MetricName,
    MetricValue,
    MutationKind,
    OutcomeMeasurements,
    OutcomeStatus,
    ReviewerObservation,
    RiskLevel,
    RolloutDecision,
    RolloutPolicy,
    RolloutStage,
    canonical_sha256,
)
from app.evaluations.publication import (
    MAX_PUBLICATION_BUNDLE_BYTES,
    PublicationAuthorization,
    PublicationAuthorizationProvider,
    PublicationRequest,
    TrustedPublicationAuthorizer,
    build_publication_bundle,
    load_publication_authorizer,
    load_publication_bundle,
)
from app.evaluations.rollout import decide_rollout, in_canary_cohort
from app.evaluations.runner import EvaluationInvocation, run_corpus
from app.evaluations.segments import (
    SegmentDimension,
    SegmentedEvaluationReport,
    build_segmented_report,
)
from app.evaluations.subprocess_executor import (
    EvaluationExecutorError,
    SubprocessEvaluationExecutor,
    public_invocation,
    sanitized_evaluation_environment,
)


SHA = "1" * 64


def _evidence(source: EvidenceSource = EvidenceSource.executor) -> EvidenceReference:
    return EvidenceReference(source=source, reference=f"evidence://{source.value}/1")


def _metric(
    value: float | None = 1.0,
    reason: str | None = None,
    *,
    source: EvidenceSource = EvidenceSource.executor,
) -> MetricValue:
    if value is None:
        return MetricValue(value=None, unavailable_reason=reason or "not measured")
    return MetricValue(value=value, evidence=[_evidence(source)])


def _execution_measurements() -> ExecutionMeasurements:
    return ExecutionMeasurements(
        requirement_coverage=_metric(1),
        build_success=_metric(1),
        lint_success=_metric(1),
        test_success=_metric(1),
        first_pass_ci_success=_metric(1, source=EvidenceSource.github_ci),
        ci_repair_success=_metric(None, "no repair was attempted"),
        failure_classification_correct=_metric(None, "no failure was classified"),
        reverted=_metric(None, "case was not merged"),
        human_correction_lines=_metric(0),
        retries=_metric(0),
        latency_seconds=_metric(12),
        cost_usd=_metric(None, "provider did not report cost"),
    )


def _invocation(workspace: Path, suffix: str = "a") -> EvaluationInvocation:
    return EvaluationInvocation(
        invocation_id=f"eval_inv_{suffix * 32}",
        ecosystem=Ecosystem.node,
        task=EvaluationTask(
            title="Implement the requested behavior",
            spec="Follow the repository contract and verify the result.",
            constraints=["Preserve existing behavior."],
            risk=RiskLevel.low,
        ),
        workspace=workspace,
    )


def _write_valid_worker(path: Path) -> None:
    measurements = _execution_measurements().model_dump(mode="json")
    script = f"""
import json
import os
import sys

invocation = json.loads(sys.stdin.read())
metadata = {{
    "input": invocation,
    "environment_keys": sorted(os.environ),
    "provider_present": bool(os.environ.get("OPENAI_API_KEY")),
    "home": os.environ.get("HOME"),
    "tmpdir": os.environ.get("TMPDIR"),
    "home_initial": sorted(os.listdir(os.environ["HOME"])),
    "tmp_initial": sorted(os.listdir(os.environ["TMPDIR"])),
}}
result = {{
    "schema_version": "evaluation_execution@2",
    "invocation_id": invocation["invocation_id"],
    "measurements": {measurements!r},
    "notes": [json.dumps(metadata, sort_keys=True)],
}}
print(json.dumps(result))
"""
    path.write_text(script, encoding="utf-8")


def _reviewer(predicted_defect: bool) -> ReviewerObservation:
    return ReviewerObservation(
        predicted_defect=predicted_defect,
        evidence=_evidence(EvidenceSource.semantic_review),
    )


def _detection(channel: DetectionChannel) -> DetectionObservation:
    return DetectionObservation(
        channel=channel,
        evidence=_evidence(EvidenceSource(channel.value)),
    )


def _harness(case_id: str, *, passed: bool) -> HarnessObservation:
    return HarnessObservation(
        fixture_id=case_id,
        fixture_sha256=SHA,
        passed=passed,
        assertions_total=1,
        assertions_passed=int(passed),
        failing_assertion_ids=[] if passed else ["expected-behavior"],
        evidence_sha256="2" * 64,
    )


def _outcome(case_id: str, status: OutcomeStatus) -> EvaluationOutcome:
    unavailable = status is OutcomeStatus.unavailable
    passed = status is OutcomeStatus.accepted
    execution = _execution_measurements()
    measurements = OutcomeMeasurements.model_validate(
        {
            **execution.model_dump(mode="json"),
            "behavioral_acceptance": (
                _metric(None, "harness did not complete").model_dump(mode="json")
                if unavailable
                else _metric(
                    float(passed), source=EvidenceSource.fixture_harness
                ).model_dump(mode="json")
            ),
        }
    )
    return EvaluationOutcome(
        case_id=case_id,
        status=status,
        detections=(
            [_detection(DetectionChannel.semantic_review)]
            if status is OutcomeStatus.detected
            else []
        ),
        reviewer=(
            None
            if unavailable
            else _reviewer(status is OutcomeStatus.detected)
        ),
        measurements=measurements,
        harness=None if unavailable else _harness(case_id, passed=passed),
        oracle_case_sha256="3" * 64,
        unavailable_reason="executor failed" if unavailable else None,
    )


def _run(
    outcomes: list[EvaluationOutcome],
    run_id: str = "run-1",
    *,
    stage: RolloutStage = RolloutStage.offline,
    model: str = "test-model@1",
    codegen_revision: str = "deadbeef",
) -> EvaluationRun:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return EvaluationRun(
        run_id=run_id,
        corpus_id="generalized-codegen-v2",
        corpus_sha256="4" * 64,
        oracle_set_sha256="5" * 64,
        fixture_sha256_by_case={outcome.case_id: SHA for outcome in outcomes},
        stage=stage,
        model=model,
        codegen_revision=codegen_revision,
        started_at=now,
        finished_at=now,
        outcomes=outcomes,
    )


def _corpus_aligned_run() -> EvaluationRun:
    corpus = load_corpus()
    oracle_set = load_oracle_set()
    outcomes = []
    for case in corpus.cases:
        payload = _outcome(case.case_id, OutcomeStatus.detected).model_dump(mode="json")
        payload["harness"]["fixture_sha256"] = case.fixture_sha256
        outcomes.append(EvaluationOutcome.model_validate(payload))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return EvaluationRun(
        run_id="corpus-aligned-run",
        corpus_id=corpus.corpus_id,
        corpus_sha256=corpus.evidence_sha256(),
        oracle_set_sha256=oracle_set.evidence_sha256(),
        fixture_sha256_by_case={
            case.case_id: case.fixture_sha256 for case in corpus.cases
        },
        stage=RolloutStage.offline,
        model="test-model@1",
        codegen_revision="deadbeef",
        started_at=now,
        finished_at=now,
        outcomes=outcomes,
    )


def test_corpus_covers_ecosystems_mutations_and_separates_oracles():
    corpus = load_corpus()
    oracle_set = load_oracle_set()

    validate_corpus_oracles(corpus, oracle_set)
    assert {case.ecosystem for case in corpus.cases} == set(Ecosystem)
    assert {case.mutation for case in corpus.cases} == set(MutationKind)
    assert len({case.case_id for case in corpus.cases}) == len(corpus.cases)
    public_payload = corpus.model_dump(mode="json")
    assert "expected_detection" not in str(public_payload)
    assert "expected_behavior" not in str(public_payload)


def test_every_digest_bound_fixture_is_a_real_failing_git_mutation():
    corpus = load_corpus()
    for case in corpus.cases:
        fixture_dir = DEFAULT_FIXTURE_ROOT / case.fixture_repo
        assert fixture_sha256(fixture_dir) == case.fixture_sha256
        with TemporaryDirectory() as temp_dir:
            materialized, manifest = materialize_fixture(
                case,
                Path(temp_dir) / "repo",
                fixture_root=DEFAULT_FIXTURE_ROOT,
            )
            assert (materialized.workspace / ".git").is_dir()
            assert not (materialized.workspace / "fixture.json").exists()
            assert not (materialized.workspace / "mutation.patch").exists()
            assert len(materialized.baseline_tree_sha256) == 64
            commits = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=materialized.workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            assert commits.stdout.strip() == "1"
            assert run_fixture_harness(materialized, manifest).passed is False


def test_strict_models_reject_unknown_fields_and_nonfinite_values():
    with pytest.raises(ValidationError):
        MetricValue(value=1, evidence=[_evidence()], surprise=True)
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            MetricValue(value=value, evidence=[_evidence()])
        with pytest.raises(ValidationError):
            RolloutPolicy(maximum_escaped_defect_rate=value)
    with pytest.raises(ValidationError):
        EvaluationExecution(
            invocation_id="eval_inv_" + "a" * 32,
            measurements=_execution_measurements(),
            notes=["x" * 4001],
        )


def test_rollout_revalidates_model_instances_before_comparing_thresholds():
    poisoned_policy = RolloutPolicy().model_copy(
        update={"minimum_requirement_coverage": math.nan}
    )
    with pytest.raises(ValidationError):
        decide_rollout(
            requested_stage=RolloutStage.reviewed_pr,
            risk=RiskLevel.low,
            summary=_passing_summary(),
            policy=poisoned_policy,
        )

    summary = _passing_summary()
    poisoned_metric = summary.mean_requirement_coverage.model_copy(
        update={"value": math.nan, "numerator": math.nan}
    )
    poisoned_summary = summary.model_copy(
        update={"mean_requirement_coverage": poisoned_metric}
    )
    with pytest.raises(ValidationError):
        decide_rollout(
            requested_stage=RolloutStage.reviewed_pr,
            risk=RiskLevel.low,
            summary=poisoned_summary,
        )


def test_metric_availability_and_ranges_are_strict():
    with pytest.raises(ValidationError):
        MetricValue(value=None)
    with pytest.raises(ValidationError):
        MetricValue(value=1)
    with pytest.raises(ValidationError):
        ExecutionMeasurements(
            **{
                **_execution_measurements().model_dump(mode="json"),
                "first_pass_ci_success": _metric(0.5).model_dump(mode="json"),
            }
        )


def test_aggregate_metrics_use_eligible_denominators_and_exact_provenance():
    run = _run(
        [
            _outcome("detected", OutcomeStatus.detected),
            _outcome("escaped", OutcomeStatus.escaped),
            _outcome("accepted", OutcomeStatus.accepted),
            _outcome("unavailable", OutcomeStatus.unavailable),
        ]
    )
    summary = aggregate_metrics(run)

    assert summary.sample_size == 4
    assert summary.detected_defect_rate.value == pytest.approx(1 / 3)
    assert summary.detected_defect_rate.denominator == 3
    assert summary.escaped_defect_rate.value == pytest.approx(1 / 3)
    assert summary.escaped_defect_rate.denominator == 3
    assert summary.behavioral_acceptance_rate.denominator == 3
    assert summary.mean_cost_usd.value is None
    assert summary.mean_cost_usd.denominator == 0
    assert len(summary.mean_cost_usd.provenance.exclusions) == 4
    assert summary.reviewer_precision.value == 1.0
    assert summary.reviewer_precision.denominator == 1
    assert summary.reviewer_recall.value == 0.5
    assert summary.reviewer_recall.denominator == 2
    assert summary.run_sha256 == run.evidence_sha256()


def test_evaluation_report_is_content_addressed_and_deterministic():
    run = _run([_outcome("detected", OutcomeStatus.detected)])
    first = build_evaluation_report(run)
    second = build_evaluation_report(run.model_copy(deep=True))

    assert first == second
    assert first.report_sha256 == second.report_sha256
    assert first.summary.run_sha256 == run.evidence_sha256()


def test_segmented_report_covers_model_ecosystem_task_type_and_risk():
    run = _corpus_aligned_run()
    corpus = load_corpus()
    first = build_segmented_report(run, corpus)
    second = build_segmented_report(run.model_copy(deep=True), corpus.model_copy(deep=True))

    assert first == second
    assert first.segmented_report_sha256 == second.segmented_report_sha256
    assert first.overall_report_sha256 == build_evaluation_report(run).report_sha256
    by_dimension = {
        dimension: [item for item in first.segments if item.dimension is dimension]
        for dimension in SegmentDimension
    }
    assert len(by_dimension[SegmentDimension.model]) == 1
    assert len(by_dimension[SegmentDimension.ecosystem]) == len(Ecosystem)
    assert len(by_dimension[SegmentDimension.task_type]) == len(MutationKind)
    assert {item.value for item in by_dimension[SegmentDimension.risk]} == {
        case.task.risk.value for case in corpus.cases
    }
    for segment in first.segments:
        for metric_name in MetricName:
            metric = getattr(segment.summary, metric_name.value)
            represented = set(metric.provenance.included_case_ids) | {
                item.case_id for item in metric.provenance.exclusions
            }
            assert represented == set(segment.case_ids)


def test_segmented_report_rejects_run_corpus_alignment_drift():
    run = _corpus_aligned_run()
    corpus = load_corpus()
    with pytest.raises(ValueError, match="corpus digest"):
        build_segmented_report(
            run.model_copy(update={"corpus_sha256": SHA}),
            corpus,
        )

    payload = run.model_dump(mode="python")
    removed = payload["outcomes"].pop()
    payload["fixture_sha256_by_case"].pop(removed["case_id"])
    subset = EvaluationRun.model_validate(payload)
    with pytest.raises(ValueError, match="cover corpus cases exactly"):
        build_segmented_report(subset, corpus)

    segmented = build_segmented_report(run, corpus)
    unsorted = segmented.model_dump(mode="json", exclude={"segmented_report_sha256"})
    unsorted["segments"] = list(reversed(unsorted["segments"]))
    with pytest.raises(ValidationError, match="deterministically sorted"):
        SegmentedEvaluationReport.model_validate(
            {
                **unsorted,
                "segmented_report_sha256": canonical_sha256(unsorted),
            }
        )


def _passing_report(
    *,
    stage: RolloutStage = RolloutStage.offline,
) -> EvaluationReport:
    outcomes = [
        *[
            _outcome(f"detected-{index}", OutcomeStatus.detected)
            for index in range(10)
        ],
        *[
            _outcome(f"accepted-{index}", OutcomeStatus.accepted)
            for index in range(20)
        ],
    ]
    return build_evaluation_report(
        _run(outcomes, run_id="passing-run", stage=stage)
    )


def _passing_summary():
    return _passing_report().summary


def _operator_bundle():
    return build_publication_bundle(
        _passing_report(),
        RolloutPolicy(
            canary_percent=100,
            minimum_behavioral_acceptance_rate=0.6,
        ),
    )


def test_offline_and_shadow_never_publish():
    for stage in (RolloutStage.offline, RolloutStage.shadow):
        decision = decide_rollout(
            requested_stage=stage,
            risk=RiskLevel.high,
            summary=None,
        )
        assert decision.allowed is True
        assert decision.publish_branch is False
        assert decision.create_pull_request is False


def test_publication_requires_thresholds_and_metric_denominators():
    summary = _passing_summary()
    policy = RolloutPolicy(minimum_behavioral_acceptance_rate=0.6)
    allowed = decide_rollout(
        requested_stage=RolloutStage.reviewed_pr,
        risk=RiskLevel.high,
        summary=summary,
        policy=policy,
    )
    assert allowed.allowed is True
    assert allowed.create_pull_request is True
    assert allowed.ready_for_review is False

    denied = decide_rollout(
        requested_stage=RolloutStage.reviewed_pr,
        risk=RiskLevel.low,
        summary=summary,
        policy=policy.model_copy(update={"minimum_metric_denominator": 11}),
    )
    assert denied.allowed is False
    assert denied.publish_branch is False
    assert any("denominator" in reason for reason in denied.reasons)


def test_rollout_decision_is_deterministic_and_canary_is_low_risk():
    summary = _passing_summary()
    policy = RolloutPolicy(
        canary_percent=100,
        minimum_behavioral_acceptance_rate=0.6,
    )
    assert in_canary_cohort("acme/repo:case", 100)
    first = decide_rollout(
        requested_stage=RolloutStage.low_risk_canary,
        risk=RiskLevel.low,
        summary=summary,
        policy=policy,
        canary_identity="acme/repo:case",
    )
    second = decide_rollout(
        requested_stage=RolloutStage.low_risk_canary,
        risk=RiskLevel.low,
        summary=summary,
        policy=policy,
        canary_identity="acme/repo:case",
    )
    assert first == second
    assert first.allowed is True
    assert first.ready_for_review is True

    denied = decide_rollout(
        requested_stage=RolloutStage.low_risk_canary,
        risk=RiskLevel.medium,
        summary=summary,
        policy=policy,
        canary_identity="acme/repo:case",
    )
    assert denied.allowed is False
    assert any("low-risk" in reason for reason in denied.reasons)


def test_rollout_decision_schema_cannot_encode_fail_open_publication():
    with pytest.raises(ValidationError):
        RolloutDecision(
            requested_stage=RolloutStage.reviewed_pr,
            risk=RiskLevel.low,
            allowed=False,
            publish_branch=True,
            create_pull_request=True,
            ready_for_review=False,
            reasons=["threshold failed"],
            policy_sha256=SHA,
            decision_sha256=SHA,
        )


def test_operator_bundle_is_content_addressed_and_loads_strictly(tmp_path: Path):
    bundle = _operator_bundle()
    path = tmp_path / "publication-bundle.json"
    path.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")

    loaded = load_publication_bundle(
        path,
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )
    authorizer = load_publication_authorizer(
        path,
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )

    assert loaded == bundle
    assert loaded.report_sha256 == loaded.report.report_sha256
    assert loaded.policy_sha256 == canonical_sha256(loaded.policy)
    assert authorizer.bundle_sha256 == loaded.bundle_sha256
    assert isinstance(authorizer, PublicationAuthorizationProvider)


def test_publication_bundle_loader_rejects_ambiguous_or_nonfinite_json(
    tmp_path: Path,
):
    path = tmp_path / "invalid.json"
    path.write_text(
        '{"schema_version":"publication_evidence_bundle@1",'
        '"schema_version":"publication_evidence_bundle@1"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_publication_bundle(
            path,
            expected_model="test-model@1",
            expected_codegen_revision="deadbeef",
        )
    path.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON value"):
        load_publication_bundle(
            path,
            expected_model="test-model@1",
            expected_codegen_revision="deadbeef",
        )


def test_publication_bundle_loader_rejects_symlink_nonregular_and_oversize(
    tmp_path: Path,
):
    target = tmp_path / "bundle.json"
    target.write_text(_operator_bundle().model_dump_json(), encoding="utf-8")
    symlink = tmp_path / "bundle-link.json"
    symlink.symlink_to(target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        load_publication_bundle(
            symlink,
            expected_model="test-model@1",
            expected_codegen_revision="deadbeef",
        )
    with pytest.raises(ValueError, match="regular file"):
        load_publication_bundle(
            tmp_path,
            expected_model="test-model@1",
            expected_codegen_revision="deadbeef",
        )
    oversized = tmp_path / "oversized.json"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_PUBLICATION_BUNDLE_BYTES + 1)
    with pytest.raises(ValueError, match="size limit"):
        load_publication_bundle(
            oversized,
            expected_model="test-model@1",
            expected_codegen_revision="deadbeef",
        )

def test_publication_bundle_is_bound_to_expected_model_and_revision(tmp_path: Path):
    path = tmp_path / "publication-bundle.json"
    path.write_text(_operator_bundle().model_dump_json(), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match expected"):
        load_publication_bundle(
            path,
            expected_model="different-model",
            expected_codegen_revision="deadbeef",
        )
    with pytest.raises(ValueError, match="expected revision"):
        load_publication_bundle(
            path,
            expected_model="test-model@1",
            expected_codegen_revision="different-revision",
        )


def test_evaluation_run_loader_is_strict_bounded_and_regular(tmp_path: Path):
    target = tmp_path / "run.json"
    run = _corpus_aligned_run()
    target.write_text(run.model_dump_json(), encoding="utf-8")
    assert load_evaluation_run(target) == run

    target.write_text('{"run_id":"first","run_id":"second"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_evaluation_run(target)
    target.write_text('{"value":Infinity}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON value"):
        load_evaluation_run(target)

    target.write_text(run.model_dump_json(), encoding="utf-8")
    symlink = tmp_path / "run-link.json"
    symlink.symlink_to(target)
    with pytest.raises(ValueError, match="must not be a symlink"):
        load_evaluation_run(symlink)
    with pytest.raises(ValueError, match="regular file"):
        load_evaluation_run(tmp_path)
    oversized = tmp_path / "oversized-run.json"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_EVALUATION_RUN_BYTES + 1)
    with pytest.raises(ValueError, match="size limit"):
        load_evaluation_run(oversized)


def test_publication_bundle_accepts_nonpublishing_stages_and_rejects_fabrication():
    shadow_bundle = build_publication_bundle(
        _passing_report(stage=RolloutStage.shadow),
        RolloutPolicy(minimum_behavioral_acceptance_rate=0.6),
    )
    assert shadow_bundle.report.run.stage is RolloutStage.shadow
    with pytest.raises(ValueError, match="non-publishing evaluation"):
        build_publication_bundle(
            _passing_report(stage=RolloutStage.reviewed_pr),
            RolloutPolicy(minimum_behavioral_acceptance_rate=0.6),
        )

    report = _passing_report()
    summary_payload = report.summary.model_dump(mode="json")
    retry_metric = dict(summary_payload["mean_retries"])
    retry_metric["numerator"] = float(retry_metric["denominator"])
    retry_metric["value"] = 1.0
    summary_payload["mean_retries"] = retry_metric
    fabricated_summary = EvaluationSummary.model_validate(summary_payload)
    report_payload = {
        "schema_version": "evaluation_report@1",
        "run": report.run.model_dump(mode="json"),
        "summary": fabricated_summary.model_dump(mode="json"),
    }
    fabricated_report = EvaluationReport.model_validate(
        {**report_payload, "report_sha256": canonical_sha256(report_payload)}
    )
    with pytest.raises(ValueError, match="summary does not match"):
        build_publication_bundle(
            fabricated_report,
            RolloutPolicy(minimum_behavioral_acceptance_rate=0.6),
        )


def test_authorizer_recomputes_and_content_addresses_each_request():
    bundle = _operator_bundle()
    authorizer = TrustedPublicationAuthorizer(
        bundle,
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )
    request = PublicationRequest(
        requested_stage=RolloutStage.reviewed_pr,
        risk=RiskLevel.high,
        model="test-model@1",
        codegen_revision="deadbeef",
    )

    first = authorizer.authorize(request)
    second = authorizer.authorize(request.model_copy(deep=True))

    assert first == second
    assert first.decision.allowed is True
    assert first.expected_model == "test-model@1"
    assert first.expected_codegen_revision == "deadbeef"
    assert first.report_sha256 == bundle.report_sha256
    assert first.bundle_sha256 == bundle.bundle_sha256
    assert first.policy_sha256 == bundle.policy_sha256
    assert first.decision.evaluation_summary_sha256 == (
        bundle.report.summary.evidence_sha256()
    )
    assert first.authorization_sha256 == canonical_sha256(
        first.model_dump(mode="json", exclude={"authorization_sha256"})
    )


def test_authorizer_recomputes_canary_identity_instead_of_trusting_a_decision():
    bundle = _operator_bundle()
    authorizer = TrustedPublicationAuthorizer(
        bundle,
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )

    def authorize(identity: str) -> PublicationAuthorization:
        return authorizer.authorize(
            PublicationRequest(
                requested_stage=RolloutStage.low_risk_canary,
                risk=RiskLevel.low,
                model="test-model@1",
                codegen_revision="deadbeef",
                canary_identity=identity,
            )
        )

    first = authorize("acme/repo:request-1")
    second = authorize("acme/repo:request-2")
    assert first.decision.allowed is True
    assert second.decision.allowed is True
    assert first.decision.canary_identity_sha256 != (
        second.decision.canary_identity_sha256
    )
    assert first.decision.decision_sha256 != second.decision.decision_sha256
    assert first.authorization_sha256 != second.authorization_sha256

    supplied_decision = first.decision
    with pytest.raises(ValidationError):
        authorizer.authorize(supplied_decision)  # type: ignore[arg-type]


def test_authorizer_rejects_request_identity_drift_and_nonpublication_stages():
    authorizer = TrustedPublicationAuthorizer(
        _operator_bundle(),
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )
    for field, value, match in (
        ("model", "different-model", "model does not match"),
        ("codegen_revision", "different-revision", "revision does not match"),
    ):
        payload = {
            "requested_stage": RolloutStage.reviewed_pr,
            "risk": RiskLevel.low,
            "model": "test-model@1",
            "codegen_revision": "deadbeef",
        }
        payload[field] = value
        with pytest.raises(ValueError, match=match):
            authorizer.authorize(PublicationRequest(**payload))

    for stage in (RolloutStage.offline, RolloutStage.shadow):
        with pytest.raises(ValidationError, match="PR publication stage"):
            PublicationRequest(
                requested_stage=stage,
                risk=RiskLevel.low,
                model="test-model@1",
                codegen_revision="deadbeef",
            )


def test_authorizer_persists_denial_without_granting_publish_capabilities():
    report = _passing_report()
    bundle = build_publication_bundle(
        report,
        RolloutPolicy(minimum_behavioral_acceptance_rate=0.99),
    )
    authorizer = TrustedPublicationAuthorizer(
        bundle,
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )
    authorization = authorizer.authorize(
        PublicationRequest(
            requested_stage=RolloutStage.reviewed_pr,
            risk=RiskLevel.low,
            model="test-model@1",
            codegen_revision="deadbeef",
        )
    )

    assert authorization.decision.allowed is False
    assert authorization.decision.publish_branch is False
    assert authorization.decision.create_pull_request is False
    assert any("behavioral acceptance" in reason for reason in authorization.decision.reasons)


def test_publication_authorization_rejects_content_address_tampering():
    authorizer = TrustedPublicationAuthorizer(
        _operator_bundle(),
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )
    authorization = authorizer.authorize(
        PublicationRequest(
            requested_stage=RolloutStage.reviewed_pr,
            risk=RiskLevel.low,
            model="test-model@1",
            codegen_revision="deadbeef",
        )
    )
    payload = authorization.model_dump(mode="json")
    payload["report_sha256"] = SHA
    with pytest.raises(ValidationError, match="authorization_sha256"):
        PublicationAuthorization.model_validate(payload)


@pytest.mark.asyncio
async def test_subprocess_executor_scrubs_credentials_and_sends_public_json_only(
    tmp_path: Path,
    monkeypatch,
):
    boundary = tmp_path / "boundary"
    workspace = boundary / "checkout"
    workspace.mkdir(parents=True)
    worker = tmp_path / "worker.py"
    _write_valid_worker(worker)
    source_environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": "/host/home-with-gh-and-ssh-config",
        "TMPDIR": "/host/tmp-with-credentials",
        "OPENAI_API_KEY": "provider-key",
        "ANTHROPIC_API_KEY": "second-provider-key",
        "GITHUB_TOKEN": "github-write-token",
        "GH_TOKEN": "gh-write-token",
        "GITHUB_APP_PRIVATE_KEY": "private-key",
        "GITHUB_APP_ID": "123",
        "APDL_INTERNAL_TOKEN": "internal-token",
        "INTERNAL_API_KEY": "internal-key",
        "POSTGRES_URL": "postgresql://secret",
        "DATABASE_URL": "postgresql://secret",
        "REDIS_URL": "redis://secret",
        "SSH_AUTH_SOCK": "/host/agent.sock",
        "CUSTOM_WRITE_TOKEN": "write-token",
    }
    sanitized = sanitized_evaluation_environment(source_environment)
    assert sanitized["OPENAI_API_KEY"] == "provider-key"
    assert sanitized["ANTHROPIC_API_KEY"] == "second-provider-key"
    assert "HOME" not in sanitized
    assert "TMPDIR" not in sanitized
    assert all(
        key not in sanitized
        for key in {
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_ID",
            "APDL_INTERNAL_TOKEN",
            "INTERNAL_API_KEY",
            "POSTGRES_URL",
            "DATABASE_URL",
            "REDIS_URL",
            "SSH_AUTH_SOCK",
            "CUSTOM_WRITE_TOKEN",
        }
    )

    async def shell_is_forbidden(*args, **kwargs):
        raise AssertionError("the evaluation executor must never invoke a shell")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", shell_is_forbidden)
    executor = SubprocessEvaluationExecutor(
        [sys.executable, str(worker)],
        environment=source_environment,
        timeout_seconds=5,
        max_output_bytes=100_000,
    )
    invocation = _invocation(workspace)
    execution = await executor.execute(invocation)
    metadata = json.loads(execution.notes[0])

    assert execution.invocation_id == invocation.invocation_id
    assert set(metadata["input"]) == {
        "schema_version",
        "invocation_id",
        "ecosystem",
        "task",
    }
    serialized = json.dumps(metadata["input"], sort_keys=True)
    assert "oracle" not in serialized
    assert "case_id" not in serialized
    assert "mutation" not in serialized
    assert "fixture" not in serialized
    assert "publish" not in serialized
    assert metadata["provider_present"] is True
    assert metadata["home_initial"] == []
    assert metadata["tmp_initial"] == []
    assert Path(metadata["home"]).parent == boundary
    assert Path(metadata["tmpdir"]).parent == boundary
    assert metadata["home"] != source_environment["HOME"]
    assert metadata["tmpdir"] != source_environment["TMPDIR"]
    assert all(
        key not in metadata["environment_keys"]
        for key in source_environment
        if key not in {
            "PATH",
            "HOME",
            "TMPDIR",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        }
    )


@pytest.mark.asyncio
async def test_subprocess_executor_rejects_candidate_output_containing_secrets(
    tmp_path: Path,
):
    measurements = _execution_measurements().model_dump(mode="json")
    workspace = tmp_path / "boundary" / "checkout"
    workspace.mkdir(parents=True)
    worker = tmp_path / "secret-worker.py"
    worker.write_text(
        "import json,os,sys\n"
        "data=json.loads(sys.stdin.read())\n"
        f"measurements={measurements!r}\n"
        "result={'schema_version':'evaluation_execution@2',"
        "'invocation_id':data['invocation_id'],'measurements':measurements,"
        "'notes':[os.environ['OPENAI_API_KEY']]}\n"
        "print(json.dumps(result))\n",
        encoding="utf-8",
    )
    executor = SubprocessEvaluationExecutor(
        [sys.executable, str(worker)],
        environment={
            "PATH": os.environ.get("PATH", os.defpath),
            "OPENAI_API_KEY": "provider-secret-material",
        },
        timeout_seconds=5,
        max_output_bytes=100_000,
    )

    with pytest.raises(EvaluationExecutorError, match="protected secret material"):
        await executor.execute(_invocation(workspace))


@pytest.mark.asyncio
async def test_subprocess_executor_rejects_strict_schema_identity_and_output_overflow(
    tmp_path: Path,
):
    measurements = _execution_measurements().model_dump(mode="json")
    scripts = {
        "unknown-field": (
            "import json,sys\n"
            "data=json.loads(sys.stdin.read())\n"
            f"result={{'schema_version':'evaluation_execution@2','invocation_id':data['invocation_id'],'measurements':{measurements!r},'surprise':True}}\n"
            "print(json.dumps(result))\n"
        ),
        "wrong-identity": (
            "import json,sys\n"
            "json.loads(sys.stdin.read())\n"
            f"result={{'schema_version':'evaluation_execution@2','invocation_id':'eval_inv_{'b' * 32}','measurements':{measurements!r}}}\n"
            "print(json.dumps(result))\n"
        ),
        "overflow": "import sys\nsys.stdin.read()\nprint('x' * 2048)\n",
    }
    for index, (name, source) in enumerate(scripts.items()):
        boundary = tmp_path / f"boundary-{index}"
        workspace = boundary / "checkout"
        workspace.mkdir(parents=True)
        worker = tmp_path / f"{name}.py"
        worker.write_text(source, encoding="utf-8")
        executor = SubprocessEvaluationExecutor(
            [sys.executable, str(worker)],
            environment={"PATH": os.environ.get("PATH", os.defpath)},
            timeout_seconds=5,
            max_output_bytes=1024 if name == "overflow" else 100_000,
        )
        expected = "output limit" if name == "overflow" else "strict JSON|invocation id"
        with pytest.raises(EvaluationExecutorError, match=expected):
            await executor.execute(_invocation(workspace, chr(ord("c") + index)))


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="process-group assertion is POSIX-specific")
async def test_subprocess_executor_timeout_kills_descendant_process_group(
    tmp_path: Path,
):
    boundary = tmp_path / "timeout-boundary"
    workspace = boundary / "checkout"
    workspace.mkdir(parents=True)
    marker = tmp_path / "descendant-survived"
    worker = tmp_path / "timeout-worker.py"
    child_source = (
        "import pathlib,time;time.sleep(0.6);"
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    worker.write_text(
        "import subprocess,sys,time\n"
        "sys.stdin.read()\n"
        f"subprocess.Popen([sys.executable, '-c', {child_source!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executor = SubprocessEvaluationExecutor(
        [sys.executable, str(worker)],
        environment={"PATH": os.environ.get("PATH", os.defpath)},
        timeout_seconds=0.1,
        max_output_bytes=4096,
    )
    with pytest.raises(EvaluationExecutorError, match="timed out"):
        await executor.execute(_invocation(workspace, "f"))
    await asyncio.sleep(0.8)
    assert not marker.exists()


def test_cli_executes_corpus_and_emits_content_addressed_reports_and_bundle(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    worker = tmp_path / "cli-worker.py"
    _write_valid_worker(worker)
    policy_path = tmp_path / "rollout-policy.json"
    policy_path.write_text(
        RolloutPolicy(
            minimum_sample_size=8,
            minimum_metric_denominator=1,
        ).model_dump_json(),
        encoding="utf-8",
    )
    run_path = tmp_path / "run.json"
    report_path = tmp_path / "report.json"
    segmented_path = tmp_path / "segmented.json"
    bundle_path = tmp_path / "bundle.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apdl-codegen-eval",
            "--executor",
            sys.executable,
            "--executor-arg",
            str(worker),
            "--stage",
            "offline",
            "--model",
            "test-model@1",
            "--codegen-revision",
            "deadbeef",
            "--run-id",
            "cli-run",
            "--rollout-policy",
            str(policy_path),
            "--run-output",
            str(run_path),
            "--report-output",
            str(report_path),
            "--segmented-output",
            str(segmented_path),
            "--bundle-output",
            str(bundle_path),
        ],
    )

    main()

    stdout = json.loads(capsys.readouterr().out)
    completed = CompletedEvaluation.model_validate(stdout["completed_evaluation"])
    run = load_evaluation_run(run_path)
    report = EvaluationReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    segmented = SegmentedEvaluationReport.model_validate_json(
        segmented_path.read_text(encoding="utf-8")
    )
    bundle = load_publication_bundle(
        bundle_path,
        expected_model="test-model@1",
        expected_codegen_revision="deadbeef",
    )
    assert completed.run == run
    assert completed.report == report
    assert completed.segmented_report == segmented
    assert segmented.overall_report_sha256 == report.report_sha256
    assert bundle.report == report
    assert stdout["publication_bundle"]["bundle_sha256"] == bundle.bundle_sha256
    assert {item.dimension for item in segmented.segments} == set(SegmentDimension)
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    assert 'apdl-codegen-eval = "app.evaluations.cli:main"' in pyproject.read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_runner_keeps_case_identity_and_oracle_out_of_executor():
    corpus = load_corpus()
    oracle_set = load_oracle_set()
    forbidden = {
        *(item.value for item in MutationKind),
        *(case.case_id for case in corpus.cases),
        *(case.fixture_repo.rsplit("/", 1)[-1] for case in corpus.cases),
        *(oracle.expected_behavior for oracle in oracle_set.oracles),
    }
    for case in corpus.cases:
        manifest = load_fixture_manifest(DEFAULT_FIXTURE_ROOT / case.fixture_repo)
        forbidden.update(assertion.assertion_id for assertion in manifest.assertions)

    class Executor:
        def __init__(self):
            self.invocations: list[EvaluationInvocation] = []
            self.exposed_text: list[str] = []

        async def execute(self, invocation: EvaluationInvocation):
            self.invocations.append(invocation)
            serialized = public_invocation(invocation).model_dump_json()
            git_log = subprocess.run(
                ["git", "log", "--format=%s"],
                cwd=invocation.workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            tree_paths = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=invocation.workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            source_suffixes = {".js", ".jsx", ".py", ".go", ".rs", ".java", ".cs"}
            non_source = []
            for relative in tree_paths:
                target = invocation.workspace / relative
                if target.suffix.lower() not in source_suffixes:
                    non_source.extend([relative, target.read_text(encoding="utf-8")])
            exposed = "\n".join(
                [serialized, str(invocation.workspace), git_log, *non_source]
            )
            self.exposed_text.append(exposed)
            assert all(item not in exposed for item in forbidden)
            return EvaluationExecution(
                invocation_id=invocation.invocation_id,
                detections=[_detection(channel) for channel in DetectionChannel],
                measurements=_execution_measurements(),
            )

    executor = Executor()
    outcomes = await run_corpus(
        corpus,
        stage=RolloutStage.shadow,
        executor=executor,
    )

    assert all(outcome.status is OutcomeStatus.detected for outcome in outcomes)
    invocation_fields = {item.name for item in fields(EvaluationInvocation)}
    assert "case_id" not in invocation_fields
    assert "mutation" not in invocation_fields
    assert "fixture_sha256" not in invocation_fields
    assert "baseline_tree_sha256" not in invocation_fields
    assert "mutation_commit_sha" not in invocation_fields
    assert "expected_detection" not in invocation_fields
    assert "expected_behavior" not in invocation_fields
    assert "oracle" not in invocation_fields
    assert "publish_branch" not in invocation_fields
    assert "create_pull_request" not in invocation_fields
    assert len(executor.exposed_text) == len(corpus.cases)


@pytest.mark.asyncio
async def test_runner_scores_only_sealed_oracle_detection_channels():
    corpus = load_corpus()

    class Executor:
        async def execute(self, invocation: EvaluationInvocation):
            return EvaluationExecution(
                invocation_id=invocation.invocation_id,
                detections=[_detection(channel) for channel in DetectionChannel],
                reviewer=_reviewer(True),
                measurements=_execution_measurements(),
            )

    outcomes = await run_corpus(
        corpus,
        stage=RolloutStage.offline,
        executor=Executor(),
    )
    assert all(outcome.status is OutcomeStatus.detected for outcome in outcomes)


@pytest.mark.asyncio
async def test_runner_rejects_every_publication_stage_before_execution():
    class NeverCalled:
        async def execute(self, invocation):  # pragma: no cover - contract assertion
            raise AssertionError("executor must not run")

    for stage in (RolloutStage.reviewed_pr, RolloutStage.low_risk_canary):
        with pytest.raises(ValueError, match="restricted to offline and shadow"):
            await run_corpus(
                load_corpus(),
                stage=stage,
                executor=NeverCalled(),
            )
