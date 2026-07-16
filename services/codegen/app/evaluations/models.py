"""Strict, evidence-carrying schemas for continuous codegen evaluation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DockerImageId = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
CaseId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")]
InvocationId = Annotated[str, Field(pattern=r"^eval_inv_[0-9a-f]{32}$")]
EvaluationNote = Annotated[str, Field(min_length=1, max_length=4000)]


class StrictModel(BaseModel):
    """Canonical evaluation boundary: reject aliases, extras, and coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


def canonical_sha256(value: BaseModel | dict | list) -> str:
    """Hash a schema artifact with stable JSON encoding."""
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class Ecosystem(str, Enum):
    node = "node"
    python = "python"
    go = "go"
    rust = "rust"
    jvm = "jvm"
    dotnet = "dotnet"


class MutationKind(str, Enum):
    asynchronous_readiness = "asynchronous_readiness"
    dependency_version_drift = "dependency_version_drift"
    dropped_props = "dropped_props"
    absent_metrics = "absent_metrics"
    missing_route = "missing_route"
    spatial_placement = "spatial_placement"
    missing_ci = "missing_ci"
    flaky_infrastructure = "flaky_infrastructure"


class DetectionChannel(str, Enum):
    contract_resolver = "contract_resolver"
    semantic_review = "semantic_review"
    github_ci = "github_ci"
    runtime_artifact = "runtime_artifact"
    remediation_classifier = "remediation_classifier"


class EvidenceSource(str, Enum):
    executor = "executor"
    contract_resolver = "contract_resolver"
    semantic_review = "semantic_review"
    github_ci = "github_ci"
    runtime_artifact = "runtime_artifact"
    remediation_classifier = "remediation_classifier"
    fixture_harness = "fixture_harness"
    human_review = "human_review"
    billing = "billing"
    post_merge = "post_merge"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class RolloutStage(str, Enum):
    offline = "offline"
    shadow = "shadow"
    development_pr = "development_pr"
    reviewed_pr = "reviewed_pr"
    low_risk_canary = "low_risk_canary"


class EvaluationTask(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    spec: str = Field(min_length=1)
    constraints: list[str] = Field(min_length=1)
    risk: RiskLevel


class EvaluationCase(StrictModel):
    """Evaluator registry entry; only task/ecosystem reach the executor."""

    schema_version: Literal["evaluation_case@2"] = "evaluation_case@2"
    case_id: CaseId
    ecosystem: Ecosystem
    fixture_repo: str = Field(pattern=r"^fixtures/[a-z0-9][a-z0-9_-]+$")
    fixture_sha256: Sha256
    mutation: MutationKind
    task: EvaluationTask


class EvaluationCorpus(StrictModel):
    schema_version: Literal["evaluation_corpus@2"] = "evaluation_corpus@2"
    corpus_id: str = Field(min_length=1)
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases_and_fixtures(self) -> EvaluationCorpus:
        ids = [case.case_id for case in self.cases]
        fixtures = [case.fixture_repo for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")
        if len(fixtures) != len(set(fixtures)):
            raise ValueError(
                "each evaluation case requires a distinct fixture repository"
            )
        return self

    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class EvaluationOracle(StrictModel):
    """Evaluator-only expectations. This model must not enter an invocation."""

    schema_version: Literal["evaluation_oracle@1"] = "evaluation_oracle@1"
    case_id: CaseId
    fixture_sha256: Sha256
    expected_detection: list[DetectionChannel] = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)


class EvaluationOracleSet(StrictModel):
    schema_version: Literal["evaluation_oracle_set@1"] = "evaluation_oracle_set@1"
    corpus_id: str = Field(min_length=1)
    oracles: list[EvaluationOracle] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> EvaluationOracleSet:
        ids = [oracle.case_id for oracle in self.oracles]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation oracle case ids must be unique")
        return self

    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class EvidenceReference(StrictModel):
    source: EvidenceSource
    reference: str = Field(min_length=1, max_length=500)
    sha256: Sha256 | None = None


class MetricValue(StrictModel):
    """A single-case measurement with explicit source evidence or absence."""

    value: float | None = Field(default=None, allow_inf_nan=False)
    unavailable_reason: str | None = Field(default=None, min_length=1)
    evidence: list[EvidenceReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_availability(self) -> MetricValue:
        if self.value is None:
            if not self.unavailable_reason:
                raise ValueError("a missing metric value requires unavailable_reason")
            if self.evidence:
                raise ValueError(
                    "an unavailable metric cannot claim measurement evidence"
                )
            return self
        if not math.isfinite(self.value):
            raise ValueError("metric values must be finite")
        if self.unavailable_reason is not None:
            raise ValueError("an available metric cannot have unavailable_reason")
        if not self.evidence:
            raise ValueError("an available metric requires source evidence")
        return self


class ExecutionMeasurements(StrictModel):
    requirement_coverage: MetricValue
    build_success: MetricValue
    lint_success: MetricValue
    test_success: MetricValue
    first_pass_ci_success: MetricValue
    ci_repair_success: MetricValue
    failure_classification_correct: MetricValue
    reverted: MetricValue
    human_correction_lines: MetricValue
    retries: MetricValue
    latency_seconds: MetricValue
    cost_usd: MetricValue

    @model_validator(mode="after")
    def constrain_measurements(self) -> ExecutionMeasurements:
        ratios = {
            "requirement_coverage": self.requirement_coverage,
            "build_success": self.build_success,
            "lint_success": self.lint_success,
            "test_success": self.test_success,
            "first_pass_ci_success": self.first_pass_ci_success,
            "ci_repair_success": self.ci_repair_success,
            "failure_classification_correct": self.failure_classification_correct,
            "reverted": self.reverted,
        }
        for name, metric in ratios.items():
            if metric.value is not None and not 0 <= metric.value <= 1:
                raise ValueError(f"{name} must be between zero and one")
        binary = {
            name: metric
            for name, metric in ratios.items()
            if name != "requirement_coverage"
        }
        for name, metric in binary.items():
            if metric.value is not None and metric.value not in {0.0, 1.0}:
                raise ValueError(f"{name} must be zero or one")
        nonnegative = {
            "human_correction_lines": self.human_correction_lines,
            "retries": self.retries,
            "latency_seconds": self.latency_seconds,
            "cost_usd": self.cost_usd,
        }
        for name, metric in nonnegative.items():
            if metric.value is not None and metric.value < 0:
                raise ValueError(f"{name} cannot be negative")
        if self.retries.value is not None and not self.retries.value.is_integer():
            raise ValueError("retries must be a whole number")
        return self


class OutcomeMeasurements(ExecutionMeasurements):
    behavioral_acceptance: MetricValue

    @model_validator(mode="after")
    def constrain_behavioral_acceptance(self) -> OutcomeMeasurements:
        if (
            self.behavioral_acceptance.value is not None
            and self.behavioral_acceptance.value not in {0.0, 1.0}
        ):
            raise ValueError("behavioral_acceptance must be zero or one")
        return self


class DetectionObservation(StrictModel):
    channel: DetectionChannel
    evidence: EvidenceReference

    @model_validator(mode="after")
    def bind_evidence_to_channel(self) -> DetectionObservation:
        if self.evidence.source.value != self.channel.value:
            raise ValueError("detection evidence source must match its channel")
        return self


class ReviewerObservation(StrictModel):
    predicted_defect: bool
    evidence: EvidenceReference

    @model_validator(mode="after")
    def require_review_evidence(self) -> ReviewerObservation:
        if self.evidence.source not in {
            EvidenceSource.semantic_review,
            EvidenceSource.human_review,
        }:
            raise ValueError("reviewer predictions require review evidence")
        return self


class EvaluationExecution(StrictModel):
    """Raw executor output. It deliberately contains no oracle expectations."""

    schema_version: Literal["evaluation_execution@2"] = "evaluation_execution@2"
    invocation_id: InvocationId
    detections: list[DetectionObservation] = Field(default_factory=list)
    reviewer: ReviewerObservation | None = None
    measurements: ExecutionMeasurements
    notes: list[EvaluationNote] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_detection_channels(self) -> EvaluationExecution:
        channels = [item.channel for item in self.detections]
        if len(channels) != len(set(channels)):
            raise ValueError("detection channels must be unique")
        return self


class HarnessObservation(StrictModel):
    schema_version: Literal["fixture_harness_observation@1"] = (
        "fixture_harness_observation@1"
    )
    fixture_id: str = Field(min_length=1)
    fixture_sha256: Sha256
    passed: bool
    assertions_total: int = Field(ge=1)
    assertions_passed: int = Field(ge=0)
    failing_assertion_ids: list[str] = Field(default_factory=list)
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_counts(self) -> HarnessObservation:
        if self.assertions_passed > self.assertions_total:
            raise ValueError("passed assertions cannot exceed total assertions")
        expected_failures = self.assertions_total - self.assertions_passed
        if len(self.failing_assertion_ids) != expected_failures:
            raise ValueError("failing assertion ids must account for every failure")
        if self.passed != (expected_failures == 0):
            raise ValueError("harness pass state must match assertion counts")
        if len(self.failing_assertion_ids) != len(set(self.failing_assertion_ids)):
            raise ValueError("failing assertion ids must be unique")
        return self


class OutcomeStatus(str, Enum):
    accepted = "accepted"
    detected = "detected"
    escaped = "escaped"
    unavailable = "unavailable"


class EvaluationOutcome(StrictModel):
    schema_version: Literal["evaluation_outcome@3"] = "evaluation_outcome@3"
    case_id: CaseId
    status: OutcomeStatus
    detections: list[DetectionObservation] = Field(default_factory=list)
    reviewer: ReviewerObservation | None = None
    measurements: OutcomeMeasurements
    harness: HarnessObservation | None
    oracle_case_sha256: Sha256
    egress_attestation_sha256: Sha256 | None = None
    unavailable_reason: str | None = Field(default=None, min_length=1)
    notes: list[EvaluationNote] = Field(default_factory=list, max_length=51)

    @model_validator(mode="after")
    def validate_status_evidence(self) -> EvaluationOutcome:
        if self.status is OutcomeStatus.unavailable:
            if self.harness is not None or not self.unavailable_reason:
                raise ValueError(
                    "unavailable outcomes require a reason and no harness result"
                )
            return self
        if self.harness is None:
            raise ValueError("measured outcomes require a harness result")
        if self.unavailable_reason is not None:
            raise ValueError("measured outcomes cannot have unavailable_reason")
        if self.status is OutcomeStatus.accepted:
            if not self.harness.passed:
                raise ValueError("accepted outcomes require a passing harness")
        elif self.harness.passed:
            raise ValueError("detected or escaped outcomes require a failing harness")
        if self.status is OutcomeStatus.detected and not self.detections:
            raise ValueError("detected outcomes require evidence-backed detections")
        if self.status is OutcomeStatus.escaped and self.detections:
            raise ValueError("escaped outcomes cannot claim a recognized detection")
        return self


class CodegenCandidateIdentity(StrictModel):
    """Content identity of code, behavior, and enforced worker egress."""

    schema_version: Literal["codegen_candidate_identity@3"] = (
        "codegen_candidate_identity@3"
    )
    controller_image_id: DockerImageId
    candidate_image_id: DockerImageId
    codegen_revision: str = Field(min_length=1)
    behavior_configuration_sha256: Sha256
    egress_policy_sha256: Sha256
    egress_proxy_image_id: DockerImageId
    egress_transport: Literal["network_none_unix_socket@1"]
    reviewed_max_concurrent_jobs: Literal[1]
    identity_sha256: Sha256

    @classmethod
    def build(
        cls,
        *,
        controller_image_id: str,
        candidate_image_id: str,
        codegen_revision: str,
        behavior_configuration_sha256: str,
        egress_policy_sha256: str,
        egress_proxy_image_id: str,
        reviewed_max_concurrent_jobs: int,
    ) -> CodegenCandidateIdentity:
        payload = {
            "schema_version": "codegen_candidate_identity@3",
            "controller_image_id": controller_image_id,
            "candidate_image_id": candidate_image_id,
            "codegen_revision": codegen_revision,
            "behavior_configuration_sha256": behavior_configuration_sha256,
            "egress_policy_sha256": egress_policy_sha256,
            "egress_proxy_image_id": egress_proxy_image_id,
            "egress_transport": "network_none_unix_socket@1",
            "reviewed_max_concurrent_jobs": reviewed_max_concurrent_jobs,
        }
        return cls.model_validate(
            {**payload, "identity_sha256": canonical_sha256(payload)}
        )

    @model_validator(mode="after")
    def validate_identity(self) -> CodegenCandidateIdentity:
        if self.codegen_revision != self.codegen_revision.strip():
            raise ValueError("candidate codegen_revision must be normalized")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"identity_sha256"})
        )
        if self.identity_sha256 != expected:
            raise ValueError("candidate identity_sha256 does not match its contents")
        return self


class EvaluationRun(StrictModel):
    schema_version: Literal["evaluation_run@5"] = "evaluation_run@5"
    run_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    corpus_sha256: Sha256
    oracle_set_sha256: Sha256
    fixture_sha256_by_case: dict[CaseId, Sha256]
    stage: RolloutStage
    model: str = Field(min_length=1)
    codegen_revision: str = Field(min_length=1)
    candidate_identity: CodegenCandidateIdentity | None
    started_at: datetime
    finished_at: datetime
    outcomes: list[EvaluationOutcome] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_provenance(self) -> EvaluationRun:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if (
            self.candidate_identity is not None
            and self.candidate_identity.codegen_revision != self.codegen_revision
        ):
            raise ValueError(
                "candidate identity revision must match the evaluation run"
            )
        outcome_ids = [outcome.case_id for outcome in self.outcomes]
        if len(outcome_ids) != len(set(outcome_ids)):
            raise ValueError("evaluation run outcome case ids must be unique")
        if set(self.fixture_sha256_by_case) != set(outcome_ids):
            raise ValueError(
                "fixture provenance must match evaluation outcome case ids exactly"
            )
        for outcome in self.outcomes:
            fixture_sha = self.fixture_sha256_by_case[outcome.case_id]
            if (
                outcome.harness is not None
                and outcome.harness.fixture_sha256 != fixture_sha
            ):
                raise ValueError("harness fixture digest does not match run provenance")
            if self.candidate_identity is None:
                if outcome.egress_attestation_sha256 is not None:
                    raise ValueError(
                        "custom evaluation outcomes cannot claim Docker egress evidence"
                    )
            elif (
                outcome.status is not OutcomeStatus.unavailable
                and outcome.egress_attestation_sha256 is None
            ):
                raise ValueError(
                    "measured Docker outcomes require trusted egress attestation"
                )
        return self

    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class MetricUnit(str, Enum):
    ratio = "ratio"
    lines = "lines"
    count = "count"
    seconds = "seconds"
    usd = "usd"


class MetricName(str, Enum):
    detected_defect_rate = "detected_defect_rate"
    escaped_defect_rate = "escaped_defect_rate"
    mean_requirement_coverage = "mean_requirement_coverage"
    build_pass_rate = "build_pass_rate"
    lint_pass_rate = "lint_pass_rate"
    test_pass_rate = "test_pass_rate"
    behavioral_acceptance_rate = "behavioral_acceptance_rate"
    first_pass_ci_success_rate = "first_pass_ci_success_rate"
    ci_repair_success_rate = "ci_repair_success_rate"
    failure_classification_accuracy = "failure_classification_accuracy"
    reviewer_precision = "reviewer_precision"
    reviewer_recall = "reviewer_recall"
    revert_frequency = "revert_frequency"
    mean_human_correction_lines = "mean_human_correction_lines"
    mean_retries = "mean_retries"
    mean_latency_seconds = "mean_latency_seconds"
    mean_cost_usd = "mean_cost_usd"


class MetricExclusion(StrictModel):
    case_id: CaseId
    reason: str = Field(min_length=1)


class MetricProvenance(StrictModel):
    run_id: str = Field(min_length=1)
    run_sha256: Sha256
    included_case_ids: list[CaseId]
    exclusions: list[MetricExclusion]

    @model_validator(mode="after")
    def disjoint_case_sets(self) -> MetricProvenance:
        included = self.included_case_ids
        excluded = [item.case_id for item in self.exclusions]
        if len(included) != len(set(included)):
            raise ValueError("included case ids must be unique")
        if len(excluded) != len(set(excluded)):
            raise ValueError("excluded case ids must be unique")
        if set(included) & set(excluded):
            raise ValueError("a case cannot be both included and excluded")
        return self


class AggregateMetric(StrictModel):
    """An auditable mean/rate whose denominator cannot be inferred or hidden."""

    metric: MetricName
    unit: MetricUnit
    value: float | None = Field(default=None, allow_inf_nan=False)
    numerator: float | None = Field(default=None, allow_inf_nan=False)
    denominator: int = Field(ge=0)
    unavailable_reason: str | None = Field(default=None, min_length=1)
    provenance: MetricProvenance

    @model_validator(mode="after")
    def validate_arithmetic(self) -> AggregateMetric:
        if self.denominator != len(self.provenance.included_case_ids):
            raise ValueError("metric denominator must equal its included case count")
        if self.denominator == 0:
            if self.value is not None or self.numerator is not None:
                raise ValueError("a zero-denominator metric cannot have a value")
            if not self.unavailable_reason:
                raise ValueError(
                    "a zero-denominator metric requires unavailable_reason"
                )
            return self
        if self.value is None or self.numerator is None:
            raise ValueError("a measured aggregate requires value and numerator")
        if not math.isfinite(self.value) or not math.isfinite(self.numerator):
            raise ValueError("aggregate values must be finite")
        if self.unavailable_reason is not None:
            raise ValueError("a measured aggregate cannot have unavailable_reason")
        expected = self.numerator / self.denominator
        if not math.isclose(self.value, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(
                "aggregate value must equal numerator divided by denominator"
            )
        if self.unit is MetricUnit.ratio and not 0 <= self.value <= 1:
            raise ValueError("ratio aggregates must be between zero and one")
        if self.numerator < 0:
            raise ValueError("aggregate numerators cannot be negative")
        return self


class EvaluationSummary(StrictModel):
    schema_version: Literal["evaluation_summary@2"] = "evaluation_summary@2"
    run_id: str = Field(min_length=1)
    run_sha256: Sha256
    sample_size: int = Field(ge=0)
    detected_defect_rate: AggregateMetric
    escaped_defect_rate: AggregateMetric
    mean_requirement_coverage: AggregateMetric
    build_pass_rate: AggregateMetric
    lint_pass_rate: AggregateMetric
    test_pass_rate: AggregateMetric
    behavioral_acceptance_rate: AggregateMetric
    first_pass_ci_success_rate: AggregateMetric
    ci_repair_success_rate: AggregateMetric
    failure_classification_accuracy: AggregateMetric
    reviewer_precision: AggregateMetric
    reviewer_recall: AggregateMetric
    revert_frequency: AggregateMetric
    mean_human_correction_lines: AggregateMetric
    mean_retries: AggregateMetric
    mean_latency_seconds: AggregateMetric
    mean_cost_usd: AggregateMetric

    @model_validator(mode="after")
    def validate_metric_identity_and_provenance(self) -> EvaluationSummary:
        expected = {
            name: MetricName(name)
            for name in type(self).model_fields
            if name in MetricName._value2member_map_
        }
        for field_name, metric_name in expected.items():
            metric = getattr(self, field_name)
            if metric.metric is not metric_name:
                raise ValueError(f"{field_name} contains the wrong metric identity")
            if (
                metric.provenance.run_id != self.run_id
                or metric.provenance.run_sha256 != self.run_sha256
            ):
                raise ValueError(
                    "all metric provenance must identify the summarized run"
                )
            represented = len(metric.provenance.included_case_ids) + len(
                metric.provenance.exclusions
            )
            if represented != self.sample_size:
                raise ValueError("each metric must account for every run outcome")
        return self

    def evidence_sha256(self) -> str:
        return canonical_sha256(self)


class EvaluationReport(StrictModel):
    schema_version: Literal["evaluation_report@4"] = "evaluation_report@4"
    run: EvaluationRun
    summary: EvaluationSummary
    report_sha256: Sha256

    @model_validator(mode="after")
    def bind_summary_to_run(self) -> EvaluationReport:
        if self.summary.run_id != self.run.run_id:
            raise ValueError("report summary run id does not match the run")
        if self.summary.run_sha256 != self.run.evidence_sha256():
            raise ValueError("report summary digest does not match the run")
        expected = canonical_sha256(
            {
                "schema_version": self.schema_version,
                "run": self.run.model_dump(mode="json"),
                "summary": self.summary.model_dump(mode="json"),
            }
        )
        if self.report_sha256 != expected:
            raise ValueError("report_sha256 does not match the report contents")
        return self


class RolloutPolicy(StrictModel):
    """Pre-publication policy for opening a draft, GitHub-reviewed PR.

    Only measurements produced by the sealed offline fixture harness may gate
    this transition.  GitHub CI, repair, reviewer calibration, human edits, and
    post-merge outcomes remain reported metrics, but they cannot be required
    before the first draft PR exists.
    """

    schema_version: Literal["rollout_policy@4"] = "rollout_policy@4"
    minimum_sample_size: int = Field(default=8, ge=1)
    minimum_metric_denominator: int = Field(default=8, ge=1)
    minimum_risk_segment_sample_size: int = Field(default=2, ge=1)
    minimum_ecosystem_segment_sample_size: int = Field(default=2, ge=1)
    minimum_task_type_segment_sample_size: int = Field(default=2, ge=1)
    maximum_escaped_defect_rate: float = Field(
        default=0.0, ge=0, le=1, allow_inf_nan=False
    )
    minimum_requirement_coverage: float = Field(
        default=1.0, ge=0, le=1, allow_inf_nan=False
    )
    minimum_behavioral_acceptance_rate: float = Field(
        default=1.0, ge=0, le=1, allow_inf_nan=False
    )
    canary_percent: int = Field(default=10, ge=0, le=100)


class RolloutDecision(StrictModel):
    schema_version: Literal["rollout_decision@3"] = "rollout_decision@3"
    requested_stage: RolloutStage
    risk: RiskLevel
    allowed: bool
    publish_branch: bool
    create_pull_request: bool
    ready_for_review: bool
    reasons: list[str] = Field(default_factory=list)
    evaluation_summary_sha256: Sha256 | None = None
    segmented_report_sha256: Sha256 | None = None
    policy_sha256: Sha256
    canary_identity_sha256: Sha256 | None = None
    canary_bucket: int | None = Field(default=None, ge=0, le=99)
    decision_sha256: Sha256

    @model_validator(mode="after")
    def enforce_publication_semantics(self) -> RolloutDecision:
        if self.requested_stage is RolloutStage.development_pr:
            raise ValueError(
                "development_pr uses the separate development publication contract"
            )
        publishing = (
            self.publish_branch or self.create_pull_request or self.ready_for_review
        )
        if not self.allowed and publishing:
            raise ValueError("a denied rollout cannot grant publication capabilities")
        if self.allowed and self.reasons:
            raise ValueError("an allowed rollout cannot contain denial reasons")
        if not self.allowed and not self.reasons:
            raise ValueError("a denied rollout requires at least one reason")
        if self.requested_stage in {RolloutStage.offline, RolloutStage.shadow}:
            if publishing:
                raise ValueError("offline and shadow stages cannot publish")
            if not self.allowed:
                raise ValueError("offline and shadow execution is always allowed")
        elif self.allowed:
            if not self.publish_branch or not self.create_pull_request:
                raise ValueError(
                    "an allowed PR stage must grant branch and PR publication"
                )
            expected_ready = self.requested_stage is RolloutStage.low_risk_canary
            if self.ready_for_review is not expected_ready:
                raise ValueError(
                    "ready-for-review is granted only to an allowed canary"
                )
            if self.evaluation_summary_sha256 is None:
                raise ValueError("publication requires an evaluation summary digest")
            if self.segmented_report_sha256 is None:
                raise ValueError("publication requires a segmented report digest")
        expected_sha = canonical_sha256(
            self.model_dump(mode="json", exclude={"decision_sha256"})
        )
        if self.decision_sha256 != expected_sha:
            raise ValueError("decision_sha256 does not match the decision contents")
        return self
