"""Deterministic metric aggregation with explicit denominators and provenance."""

from __future__ import annotations

from collections.abc import Callable

from app.evaluations.models import (
    AggregateMetric,
    EvaluationOutcome,
    EvaluationReport,
    EvaluationRun,
    EvaluationSummary,
    MetricExclusion,
    MetricName,
    MetricProvenance,
    MetricUnit,
    MetricValue,
    OutcomeStatus,
    canonical_sha256,
)


MeasurementGetter = Callable[[EvaluationOutcome], MetricValue]


def _provenance(
    run: EvaluationRun,
    *,
    included: list[str],
    exclusions: list[MetricExclusion],
) -> MetricProvenance:
    return MetricProvenance(
        run_id=run.run_id,
        run_sha256=run.evidence_sha256(),
        included_case_ids=included,
        exclusions=exclusions,
    )


def _aggregate(
    run: EvaluationRun,
    *,
    metric: MetricName,
    unit: MetricUnit,
    values: list[tuple[str, float]],
    exclusions: list[MetricExclusion],
    missing_reason: str,
) -> AggregateMetric:
    included = [case_id for case_id, _ in values]
    if not values:
        return AggregateMetric(
            metric=metric,
            unit=unit,
            value=None,
            numerator=None,
            denominator=0,
            unavailable_reason=missing_reason,
            provenance=_provenance(run, included=[], exclusions=exclusions),
        )
    numerator = sum(value for _, value in values)
    denominator = len(values)
    return AggregateMetric(
        metric=metric,
        unit=unit,
        value=numerator / denominator,
        numerator=numerator,
        denominator=denominator,
        provenance=_provenance(run, included=included, exclusions=exclusions),
    )


def _measurement_mean(
    run: EvaluationRun,
    *,
    metric: MetricName,
    unit: MetricUnit,
    getter: MeasurementGetter,
    missing_reason: str,
) -> AggregateMetric:
    values: list[tuple[str, float]] = []
    exclusions: list[MetricExclusion] = []
    for outcome in run.outcomes:
        measurement = getter(outcome)
        if measurement.value is None:
            exclusions.append(
                MetricExclusion(
                    case_id=outcome.case_id,
                    reason=measurement.unavailable_reason or "measurement unavailable",
                )
            )
        else:
            values.append((outcome.case_id, measurement.value))
    return _aggregate(
        run,
        metric=metric,
        unit=unit,
        values=values,
        exclusions=exclusions,
        missing_reason=missing_reason,
    )


def _defect_rate(run: EvaluationRun, metric: MetricName) -> AggregateMetric:
    values: list[tuple[str, float]] = []
    exclusions: list[MetricExclusion] = []
    for outcome in run.outcomes:
        if outcome.status in {
            OutcomeStatus.accepted,
            OutcomeStatus.detected,
            OutcomeStatus.escaped,
        }:
            observed = (
                bool(outcome.detections)
                if metric is MetricName.detected_defect_rate
                else outcome.status is OutcomeStatus.escaped
            )
            values.append((outcome.case_id, float(observed)))
        else:
            exclusions.append(
                MetricExclusion(
                    case_id=outcome.case_id,
                    reason=outcome.unavailable_reason or "outcome unavailable",
                )
            )
    return _aggregate(
        run,
        metric=metric,
        unit=MetricUnit.ratio,
        values=values,
        exclusions=exclusions,
        missing_reason="no behaviorally labeled candidates were measured",
    )


def _reviewer_metric(run: EvaluationRun, metric: MetricName) -> AggregateMetric:
    values: list[tuple[str, float]] = []
    exclusions: list[MetricExclusion] = []
    for outcome in run.outcomes:
        if outcome.harness is None:
            exclusions.append(
                MetricExclusion(
                    case_id=outcome.case_id,
                    reason=outcome.unavailable_reason or "behavioral label unavailable",
                )
            )
            continue
        if outcome.reviewer is None:
            exclusions.append(
                MetricExclusion(
                    case_id=outcome.case_id,
                    reason="no reviewer prediction was recorded",
                )
            )
            continue
        actual_defect = not outcome.harness.passed
        predicted_defect = outcome.reviewer.predicted_defect
        if metric is MetricName.reviewer_precision:
            if not predicted_defect:
                exclusions.append(
                    MetricExclusion(
                        case_id=outcome.case_id,
                        reason="case was not a reviewer-predicted positive",
                    )
                )
                continue
            values.append((outcome.case_id, float(actual_defect)))
        else:
            if not actual_defect:
                exclusions.append(
                    MetricExclusion(
                        case_id=outcome.case_id,
                        reason="case was not an oracle-labeled defect",
                    )
                )
                continue
            values.append((outcome.case_id, float(predicted_defect)))
    missing_reason = (
        "no reviewer-predicted positives had behavioral labels"
        if metric is MetricName.reviewer_precision
        else "no behaviorally defective cases had reviewer predictions"
    )
    return _aggregate(
        run,
        metric=metric,
        unit=MetricUnit.ratio,
        values=values,
        exclusions=exclusions,
        missing_reason=missing_reason,
    )


def aggregate_metrics(run: EvaluationRun) -> EvaluationSummary:
    """Aggregate only evidence-backed finite measurements from one immutable run."""
    run = EvaluationRun.model_validate(run.model_dump(mode="python"))
    return EvaluationSummary(
        run_id=run.run_id,
        run_sha256=run.evidence_sha256(),
        sample_size=len(run.outcomes),
        detected_defect_rate=_defect_rate(run, MetricName.detected_defect_rate),
        escaped_defect_rate=_defect_rate(run, MetricName.escaped_defect_rate),
        mean_requirement_coverage=_measurement_mean(
            run,
            metric=MetricName.mean_requirement_coverage,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.requirement_coverage,
            missing_reason="requirement coverage was not measured",
        ),
        build_pass_rate=_measurement_mean(
            run,
            metric=MetricName.build_pass_rate,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.build_success,
            missing_reason="build outcomes were not measured",
        ),
        lint_pass_rate=_measurement_mean(
            run,
            metric=MetricName.lint_pass_rate,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.lint_success,
            missing_reason="lint outcomes were not measured",
        ),
        test_pass_rate=_measurement_mean(
            run,
            metric=MetricName.test_pass_rate,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.test_success,
            missing_reason="test outcomes were not measured",
        ),
        behavioral_acceptance_rate=_measurement_mean(
            run,
            metric=MetricName.behavioral_acceptance_rate,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.behavioral_acceptance,
            missing_reason="behavioral harness outcomes were not measured",
        ),
        first_pass_ci_success_rate=_measurement_mean(
            run,
            metric=MetricName.first_pass_ci_success_rate,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.first_pass_ci_success,
            missing_reason="GitHub CI did not report first-pass outcomes",
        ),
        ci_repair_success_rate=_measurement_mean(
            run,
            metric=MetricName.ci_repair_success_rate,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.ci_repair_success,
            missing_reason="no CI repair attempts were measured",
        ),
        failure_classification_accuracy=_measurement_mean(
            run,
            metric=MetricName.failure_classification_accuracy,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.failure_classification_correct,
            missing_reason="failure classifications were not oracle-labeled",
        ),
        reviewer_precision=_reviewer_metric(run, MetricName.reviewer_precision),
        reviewer_recall=_reviewer_metric(run, MetricName.reviewer_recall),
        revert_frequency=_measurement_mean(
            run,
            metric=MetricName.revert_frequency,
            unit=MetricUnit.ratio,
            getter=lambda item: item.measurements.reverted,
            missing_reason="post-merge revert outcomes were not measured",
        ),
        mean_human_correction_lines=_measurement_mean(
            run,
            metric=MetricName.mean_human_correction_lines,
            unit=MetricUnit.lines,
            getter=lambda item: item.measurements.human_correction_lines,
            missing_reason="human correction size was not measured",
        ),
        mean_retries=_measurement_mean(
            run,
            metric=MetricName.mean_retries,
            unit=MetricUnit.count,
            getter=lambda item: item.measurements.retries,
            missing_reason="retry counts were not measured",
        ),
        mean_latency_seconds=_measurement_mean(
            run,
            metric=MetricName.mean_latency_seconds,
            unit=MetricUnit.seconds,
            getter=lambda item: item.measurements.latency_seconds,
            missing_reason="latency was not measured",
        ),
        mean_cost_usd=_measurement_mean(
            run,
            metric=MetricName.mean_cost_usd,
            unit=MetricUnit.usd,
            getter=lambda item: item.measurements.cost_usd,
            missing_reason="the editor did not expose reliable cost data",
        ),
    )


def build_evaluation_report(run: EvaluationRun) -> EvaluationReport:
    """Build the same report bytes for the same strict run artifact."""
    run = EvaluationRun.model_validate(run.model_dump(mode="python"))
    summary = aggregate_metrics(run)
    report_payload = {
        "schema_version": "evaluation_report@1",
        "run": run.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
    }
    return EvaluationReport(
        run=run,
        summary=summary,
        report_sha256=canonical_sha256(report_payload),
    )
