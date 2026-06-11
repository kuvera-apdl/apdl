"""Auto-rollback logic for agent-deployed experiments.

Monitors key metrics after an experiment is deployed and triggers
automatic rollback if degradation thresholds are breached.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
import httpx

logger = logging.getLogger(__name__)

QUERY_SERVICE_URL = os.getenv("QUERY_SERVICE_URL", "http://localhost:8082")
CONFIG_SERVICE_URL = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
_TIMEOUT = 15.0


@dataclass
class RollbackThresholds:
    """Thresholds that trigger automatic rollback.

    If any metric breaches its threshold compared to the pre-experiment
    baseline, the experiment is rolled back.
    """
    error_rate_increase: float = 0.005     # +0.5 percentage points
    p95_latency_increase: float = 0.20     # +20%
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
    """Monitors a deployed experiment and triggers rollback on degradation.

    Usage:
        monitor = ExperimentRollbackMonitor(thresholds=RollbackThresholds())
        decision = await monitor.evaluate(
            project_id="default",
            experiment_id="exp_checkout_v2",
            flag_key="exp_checkout_v2",
            baseline=MetricSnapshot(error_rate=0.01, p95_latency_ms=200, primary_metric_value=0.12),
        )
        if decision.should_rollback:
            await monitor.execute_rollback(
                project_id="your-project",
                flag_key="exp_checkout_v2",
            )
    """

    def __init__(self, thresholds: RollbackThresholds | None = None) -> None:
        self.thresholds = thresholds or RollbackThresholds()

    async def evaluate(
        self,
        project_id: str,
        experiment_id: str,
        flag_key: str,
        baseline: MetricSnapshot,
        primary_metric_event: str = "purchase",
    ) -> RollbackDecision:
        """Evaluate whether an experiment should be rolled back.

        Compares current metric values against the baseline snapshot
        and the configured thresholds.

        Args:
            project_id: Project scope.
            experiment_id: The experiment to evaluate.
            flag_key: The feature flag controlling the experiment.
            baseline: Pre-experiment metric snapshot.
            primary_metric_event: Event name for the primary metric.

        Returns:
            A RollbackDecision indicating whether rollback is needed.
        """
        reasons: list[str] = []

        # Fetch current experiment results
        current = await self._fetch_current_metrics(
            project_id, experiment_id, flag_key, primary_metric_event
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
                (current.p95_latency_ms - baseline.p95_latency_ms) / baseline.p95_latency_ms
            )
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
        """Execute a rollback by disabling the experiment's feature flag.

        This forces all users to see the control variant.

        Args:
            project_id: Project containing the feature flag.
            flag_key: The feature flag to disable.

        Returns:
            True if rollback was successful, False otherwise.
        """
        try:
            async with httpx.AsyncClient(
                base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT
            ) as client:
                resp = await client.post(
                    f"/v1/admin/flags/{flag_key}/disable",
                    params={"project_id": project_id},
                    json={
                        "reason": "experiment_rollback",
                        "source": "system",
                        "evidence": {
                            "rollback_monitor": "experiment",
                        },
                    },
                )
                resp.raise_for_status()

            logger.info("Rollback executed: flag %s disabled", flag_key)
            return True
        except Exception as exc:
            logger.error("Rollback failed for flag %s: %s", flag_key, exc)
            return False

    async def _fetch_current_metrics(
        self,
        project_id: str,
        experiment_id: str,
        flag_key: str,
        primary_metric_event: str,
    ) -> MetricSnapshot:
        """Fetch current metric values from the query service.

        In a production system, error_rate and p95_latency would come from
        a monitoring system (e.g., Prometheus). Here we approximate using
        the query service experiment results.
        """
        try:
            async with httpx.AsyncClient(
                base_url=QUERY_SERVICE_URL, timeout=_TIMEOUT
            ) as client:
                resp = await client.get(
                    f"/v1/query/experiment/{experiment_id}",
                    params={
                        "metric": primary_metric_event,
                        "project_id": project_id,
                        "flag_key": flag_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            # Extract treatment variant metrics
            variants = data.get("variants", [])
            treatment_variants = [v for v in variants if v.get("variant") != "control"]

            if treatment_variants:
                treatment = treatment_variants[0]
                primary_value = treatment.get("mean", 0.0)
            else:
                primary_value = 0.0

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
            return MetricSnapshot()
