"""Experiment management tools — wrappers around Config and Query Service APIs."""

from __future__ import annotations

import os
from typing import Any

import httpx

QUERY_SERVICE_URL = os.getenv("QUERY_SERVICE_URL", "http://localhost:8082")
CONFIG_SERVICE_URL = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
_TIMEOUT = 30.0


async def get_active_experiments(project_id: str) -> list[dict[str, Any]]:
    """Get all active experiments for a project from the config service.

    Args:
        project_id: The project to query.

    Returns:
        List of active experiment configurations.
    """
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get("/v1/experiments", params={"project_id": project_id})
        resp.raise_for_status()
        return resp.json()


async def create_experiment_config(
    project_id: str,
    experiment_id: str,
    hypothesis: str,
    variants: list[dict[str, Any]],
    primary_metric: dict[str, str],
    secondary_metrics: list[dict[str, str]] | None = None,
    guardrail_metrics: list[dict[str, Any]] | None = None,
    targeting: dict[str, Any] | None = None,
    estimated_duration_days: int = 14,
    flag_key: str | None = None,
) -> dict[str, Any]:
    """Create a new experiment configuration and its associated feature flag.

    This sets up both the experiment metadata and the underlying flag
    that controls variant assignment.

    Args:
        project_id: Project scope.
        experiment_id: Unique experiment identifier (e.g. "exp_checkout_v2").
        hypothesis: The hypothesis being tested.
        variants: Variant definitions with "key", "weight", and "description".
        primary_metric: Dict with "event", "type", and "direction".
        secondary_metrics: Optional additional metrics to track.
        guardrail_metrics: Metrics that should not degrade.
        targeting: User targeting conditions.
        estimated_duration_days: Expected experiment runtime.
        flag_key: Feature flag key to use (defaults to experiment_id).

    Returns:
        The created experiment configuration.
    """
    payload: dict[str, Any] = {
        "project_id": project_id,
        "experiment_id": experiment_id,
        "hypothesis": hypothesis,
        "variants": variants,
        "primary_metric": primary_metric,
        "flag_key": flag_key or experiment_id,
        "estimated_duration_days": estimated_duration_days,
    }
    if secondary_metrics:
        payload["secondary_metrics"] = secondary_metrics
    if guardrail_metrics:
        payload["guardrail_metrics"] = guardrail_metrics
    if targeting:
        payload["targeting"] = targeting

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post("/v1/admin/experiments", json=payload)
        resp.raise_for_status()
        return resp.json()


async def calculate_sample_size(
    baseline_rate: float,
    minimum_detectable_effect: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> dict[str, Any]:
    """Calculate the required sample size per variant for an experiment.

    Uses the Query Service's statistical engine.

    Args:
        baseline_rate: Expected conversion rate for control (e.g. 0.10 for 10%).
        minimum_detectable_effect: Smallest absolute effect to detect (e.g. 0.02 for 2pp).
        alpha: Significance level (default 0.05).
        power: Statistical power (default 0.8).

    Returns:
        Dict with "sample_size_per_variant" and input parameters.
    """
    # Use the inline statistical engine to avoid a network round-trip
    analyzer = _AnalyzerRef.get()
    n = analyzer.calculate_sample_size(baseline_rate, minimum_detectable_effect, alpha, power)
    return {
        "sample_size_per_variant": n,
        "baseline_rate": baseline_rate,
        "minimum_detectable_effect": minimum_detectable_effect,
        "alpha": alpha,
        "power": power,
    }


async def get_experiment_results(
    experiment_id: str,
    metric: str,
    project_id: str = "default",
    method: str = "frequentist",
) -> dict[str, Any]:
    """Get statistical results for a running or completed experiment.

    Args:
        experiment_id: The experiment to analyse.
        metric: The conversion/metric event to evaluate.
        project_id: Project ID.
        method: Statistical method — "frequentist", "bayesian", or "sequential".

    Returns:
        Full experiment analysis results.
    """
    async with httpx.AsyncClient(base_url=QUERY_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"/v1/query/experiment/{experiment_id}",
            params={"metric": metric, "method": method, "project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()


# Inline reference to avoid circular import — provides ExperimentAnalyzer
# when used outside the query service process.
class _AnalyzerRef:
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            # Import from the query service's statistical module if available,
            # otherwise fall back to a minimal inline implementation.
            try:
                from app.models.statistics import ExperimentAnalyzer
                cls._instance = ExperimentAnalyzer()
            except ImportError:
                cls._instance = _InlineAnalyzer()
        return cls._instance


class _InlineAnalyzer:
    """Minimal sample-size calculator for use within the agents service."""

    def calculate_sample_size(
        self, baseline_rate: float, mde: float,
        alpha: float = 0.05, power: float = 0.8,
    ) -> int:
        import math
        from scipy import stats as sp_stats
        p1, p2 = baseline_rate, baseline_rate + mde
        p_bar = (p1 + p2) / 2.0
        z_alpha = sp_stats.norm.ppf(1 - alpha / 2)
        z_beta = sp_stats.norm.ppf(power)
        num = (z_alpha * math.sqrt(2 * p_bar * (1 - p_bar))
               + z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
        return math.ceil(num / (mde ** 2)) if mde != 0 else 0

