"""Fail-closed, deterministic staged-rollout decisions for codegen changes."""

from __future__ import annotations

import hashlib

from app.evaluations.models import (
    AggregateMetric,
    EvaluationSummary,
    RiskLevel,
    RolloutDecision,
    RolloutPolicy,
    RolloutStage,
    canonical_sha256,
)


def canary_bucket(identity: str) -> int:
    if not identity:
        raise ValueError("canary identity cannot be empty")
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") % 100


def in_canary_cohort(identity: str, percent: int) -> bool:
    """Assign an identity to a stable 0-99 bucket without mutable state."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return canary_bucket(identity) < percent


def _metric_at_least(
    metric: AggregateMetric,
    minimum: float,
    label: str,
    minimum_denominator: int,
    reasons: list[str],
) -> None:
    if metric.value is None:
        reasons.append(f"{label} is unavailable: {metric.unavailable_reason}")
        return
    if metric.denominator < minimum_denominator:
        reasons.append(
            f"{label} denominator {metric.denominator} is below required "
            f"{minimum_denominator}"
        )
    if metric.value < minimum:
        reasons.append(f"{label} {metric.value:.3f} is below required {minimum:.3f}")


def _build_decision(**values) -> RolloutDecision:
    payload = {"schema_version": "rollout_decision@2", **values}
    return RolloutDecision(
        **payload,
        decision_sha256=canonical_sha256(payload),
    )


def decide_rollout(
    *,
    requested_stage: RolloutStage,
    risk: RiskLevel,
    summary: EvaluationSummary | None,
    policy: RolloutPolicy | None = None,
    canary_identity: str | None = None,
) -> RolloutDecision:
    """Authorize publication from finite metrics with auditable denominators."""
    resolved = (
        RolloutPolicy.model_validate(policy.model_dump(mode="python"))
        if policy is not None
        else RolloutPolicy()
    )
    validated_summary = (
        EvaluationSummary.model_validate(summary.model_dump(mode="python"))
        if summary is not None
        else None
    )
    summary = validated_summary
    policy_sha256 = canonical_sha256(resolved)
    bucket = canary_bucket(canary_identity) if canary_identity else None
    identity_sha = (
        hashlib.sha256(canary_identity.encode()).hexdigest()
        if canary_identity
        else None
    )
    if requested_stage in {RolloutStage.offline, RolloutStage.shadow}:
        return _build_decision(
            requested_stage=requested_stage,
            risk=risk,
            allowed=True,
            publish_branch=False,
            create_pull_request=False,
            ready_for_review=False,
            reasons=[],
            evaluation_summary_sha256=(
                summary.evidence_sha256() if summary is not None else None
            ),
            policy_sha256=policy_sha256,
            canary_identity_sha256=identity_sha,
            canary_bucket=bucket,
        )

    reasons: list[str] = []
    if summary is None:
        reasons.append("no evaluation summary was supplied")
    else:
        if summary.sample_size < resolved.minimum_sample_size:
            reasons.append(
                f"sample size {summary.sample_size} is below required "
                f"{resolved.minimum_sample_size}"
            )
        escaped = summary.escaped_defect_rate
        if escaped.value is None:
            reasons.append(
                "escaped defect rate is unavailable: "
                f"{escaped.unavailable_reason}"
            )
        else:
            if escaped.denominator < resolved.minimum_metric_denominator:
                reasons.append(
                    f"escaped defect rate denominator {escaped.denominator} is below "
                    f"required {resolved.minimum_metric_denominator}"
                )
            if escaped.value > resolved.maximum_escaped_defect_rate:
                reasons.append(
                    f"escaped defect rate {escaped.value:.3f} exceeds maximum "
                    f"{resolved.maximum_escaped_defect_rate:.3f}"
                )
        gates = (
            (
                summary.mean_requirement_coverage,
                resolved.minimum_requirement_coverage,
                "requirement coverage",
            ),
            (
                summary.build_pass_rate,
                resolved.minimum_build_pass_rate,
                "build pass rate",
            ),
            (
                summary.test_pass_rate,
                resolved.minimum_test_pass_rate,
                "test pass rate",
            ),
            (
                summary.behavioral_acceptance_rate,
                resolved.minimum_behavioral_acceptance_rate,
                "behavioral acceptance rate",
            ),
            (
                summary.first_pass_ci_success_rate,
                resolved.minimum_first_pass_ci_success_rate,
                "first-pass CI success rate",
            ),
            (
                summary.reviewer_precision,
                resolved.minimum_reviewer_precision,
                "reviewer precision",
            ),
            (
                summary.reviewer_recall,
                resolved.minimum_reviewer_recall,
                "reviewer recall",
            ),
        )
        for metric, minimum, label in gates:
            _metric_at_least(
                metric,
                minimum,
                label,
                resolved.minimum_metric_denominator,
                reasons,
            )

    if requested_stage is RolloutStage.low_risk_canary:
        if risk is not RiskLevel.low:
            reasons.append("only low-risk changes may enter the canary stage")
        if not canary_identity:
            reasons.append("a stable canary identity is required")
        elif bucket is not None and bucket >= resolved.canary_percent:
            reasons.append("identity is outside the configured canary cohort")

    allowed = not reasons
    return _build_decision(
        requested_stage=requested_stage,
        risk=risk,
        allowed=allowed,
        publish_branch=allowed,
        create_pull_request=allowed,
        ready_for_review=allowed and requested_stage is RolloutStage.low_risk_canary,
        reasons=reasons,
        evaluation_summary_sha256=(
            summary.evidence_sha256() if summary is not None else None
        ),
        policy_sha256=policy_sha256,
        canary_identity_sha256=identity_sha,
        canary_bucket=bucket,
    )
