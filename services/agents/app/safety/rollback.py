"""Fail-closed rollback surface for experiment decision snapshots."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

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
    """Decline rollback assessment until readiness evidence is implemented.

    Usage:
        monitor = ExperimentRollbackMonitor(thresholds=RollbackThresholds())
        decision = await monitor.evaluate(
            project_id="default",
            experiment_id="exp_checkout_v2",
            baseline=MetricSnapshot(error_rate=0.01, p95_latency_ms=200, primary_metric_value=0.12),
        )
        # Decision snapshots explicitly report deployment_readiness=not_assessed.
    """

    def __init__(self, thresholds: RollbackThresholds | None = None) -> None:
        self.thresholds = thresholds or RollbackThresholds()

    async def evaluate(
        self,
        project_id: str,
        experiment_id: str,
        baseline: MetricSnapshot,
    ) -> RollbackDecision:
        """Return an unavailable decision without interpreting significance.

        Args:
            project_id: Project scope.
            experiment_id: The experiment to evaluate.
            baseline: Pre-experiment metric snapshot.

        Returns:
            A fail-closed result. No Query request or recommendation is made.
        """
        return RollbackDecision(
            should_rollback=False,
            reasons=[
                "Rollback assessment unavailable: experiment snapshots do not "
                "verify data completeness or deployment readiness."
            ],
            baseline=baseline,
            current=None,
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
