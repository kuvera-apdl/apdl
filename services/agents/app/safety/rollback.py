"""Read-only rollback assessment for agent-designed experiments.

The OSS developer preview may assess degradation, but automatic Config
mutation is deliberately unavailable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from app.service_auth import service_headers

logger = logging.getLogger(__name__)

QUERY_SERVICE_URL = os.getenv("QUERY_SERVICE_URL", "http://localhost:8082")
_TIMEOUT = 15.0


@dataclass
class RollbackThresholds:
    """Thresholds that trigger automatic rollback.

    If any metric breaches its threshold compared to the pre-experiment
    baseline, the experiment is rolled back.
    """

    error_rate_increase: float = 0.005  # +0.5 percentage points
    p95_latency_increase: float = 0.20  # +20%
    primary_metric_decrease: float = 0.02  # -2 percentage points


@dataclass
class MetricSnapshot:
    """A point-in-time metric reading."""

    error_rate: float = 0.0
    p95_latency_ms: float = 0.0
    primary_metric_value: float = 0.0


@dataclass
class RollbackDecision:
    """Result of a rollback evaluation."""

    should_rollback: bool
    reasons: list[str] = field(default_factory=list)
    baseline: MetricSnapshot | None = None
    current: MetricSnapshot | None = None


class ExperimentRollbackMonitor:
    """Assess a deployed experiment without performing automatic mutation.

    Usage:
        monitor = ExperimentRollbackMonitor(thresholds=RollbackThresholds())
        decision = await monitor.evaluate(
            project_id="default",
            experiment_id="exp_checkout_v2",
            baseline=MetricSnapshot(error_rate=0.01, p95_latency_ms=200, primary_metric_value=0.12),
        )
        # A human operator reviews the recommendation and uses Config's
        # versioned experiment lifecycle endpoint when action is warranted.
    """

    def __init__(self, thresholds: RollbackThresholds | None = None) -> None:
        self.thresholds = thresholds or RollbackThresholds()

    async def evaluate(
        self,
        project_id: str,
        experiment_id: str,
        baseline: MetricSnapshot,
    ) -> RollbackDecision:
        """Evaluate whether an experiment should be rolled back.

        Compares current metric values against the baseline snapshot
        and the configured thresholds.

        Args:
            project_id: Project scope.
            experiment_id: The experiment to evaluate.
            baseline: Pre-experiment metric snapshot.

        Returns:
            A RollbackDecision indicating whether rollback is needed.
        """
        reasons: list[str] = []

        # Fetch current experiment results. A fetch failure returns None and
        # must NOT trigger rollback: fabricated zero metrics would make any
        # positive baseline look like total degradation, disabling a healthy
        # experiment because the query service blipped.
        current = await self._fetch_current_metrics(project_id, experiment_id)
        if current is None:
            logger.warning(
                "Cannot evaluate rollback for experiment %s — current metrics "
                "unavailable; skipping this evaluation.",
                experiment_id,
            )
            return RollbackDecision(
                should_rollback=False,
                reasons=["Current metrics unavailable — evaluation skipped."],
                baseline=baseline,
                current=None,
            )

        # Check error rate
        error_delta = current.error_rate - baseline.error_rate
        if error_delta > self.thresholds.error_rate_increase:
            reasons.append(
                f"Error rate increased by {error_delta:.4f} "
                f"(threshold: {self.thresholds.error_rate_increase:.4f})"
            )

        # Check p95 latency
        if baseline.p95_latency_ms > 0:
            latency_increase = (
                current.p95_latency_ms - baseline.p95_latency_ms
            ) / baseline.p95_latency_ms
            if latency_increase > self.thresholds.p95_latency_increase:
                reasons.append(
                    f"p95 latency increased by {latency_increase:.1%} "
                    f"(threshold: {self.thresholds.p95_latency_increase:.1%})"
                )

        # Check primary metric
        if baseline.primary_metric_value > 0:
            metric_delta = baseline.primary_metric_value - current.primary_metric_value
            if metric_delta > self.thresholds.primary_metric_decrease:
                reasons.append(
                    f"Primary metric decreased by {metric_delta:.4f} "
                    f"(threshold: {self.thresholds.primary_metric_decrease:.4f})"
                )

        should_rollback = len(reasons) > 0

        if should_rollback:
            logger.warning(
                "Rollback recommended for experiment %s: %s",
                experiment_id,
                "; ".join(reasons),
            )

        return RollbackDecision(
            should_rollback=should_rollback,
            reasons=reasons,
            baseline=baseline,
            current=current,
        )

    async def execute_rollback(self, project_id: str, flag_key: str) -> bool:
        """Decline automatic rollback in the OSS developer preview.

        Experiment-owned flags can only be changed through their owning
        experiment's versioned lifecycle command. This legacy monitor does not
        own that authoritative version, so it must never attempt a generic flag
        mutation.

        Args:
            project_id: Project containing the feature flag.
            flag_key: The feature flag to disable.

        Returns:
            Always ``False`` while automatic decisions are unsupported.
        """
        logger.warning(
            "Automatic experiment rollback is disabled for project %s flag %s",
            project_id,
            flag_key,
        )
        return False

    async def _fetch_current_metrics(
        self,
        project_id: str,
        experiment_id: str,
    ) -> MetricSnapshot | None:
        """Fetch current metric values from the query service.

        Returns None when metrics are unavailable (fetch failed, or no
        treatment variant data yet) — the caller must treat that as
        "cannot evaluate", never as zeros.

        In a production system, error_rate and p95_latency would come from
        a monitoring system (e.g., Prometheus). Here we approximate using
        the query service experiment results.
        """
        try:
            async with httpx.AsyncClient(
                base_url=QUERY_SERVICE_URL,
                timeout=_TIMEOUT,
                headers=service_headers(project_id),
            ) as client:
                resp = await client.get(
                    f"/v1/query/experiment/{quote(experiment_id, safe='')}",
                    params={"project_id": project_id},
                )
                resp.raise_for_status()
                data = resp.json()

            if data.get("analysis_status") != "ready":
                return None
            default_variant = data.get("control_variant")
            variants = data.get("arms", [])
            treatment_variants = [
                v for v in variants if v.get("variant") != default_variant
            ]

            if not treatment_variants:
                # No exposure data yet — indistinguishable from a broken
                # experiment only by fabricating numbers, so don't.
                return None
            primary_value = min(
                float(variant.get("conversion_rate", 0.0))
                for variant in treatment_variants
            )

            # Error rate and latency would come from a monitoring stack.
            # For this implementation, we return placeholder values that
            # should be replaced by real monitoring integration.
            return MetricSnapshot(
                error_rate=0.0,  # would come from Prometheus/Datadog
                p95_latency_ms=0.0,  # would come from Prometheus/Datadog
                primary_metric_value=primary_value,
            )

        except Exception as exc:
            logger.error("Failed to fetch current metrics: %s", exc)
            return None
