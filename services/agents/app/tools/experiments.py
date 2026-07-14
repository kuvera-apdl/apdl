"""Experiment management tools — wrappers around Config and Query Service APIs."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.service_auth import service_headers

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
    async with httpx.AsyncClient(
        base_url=CONFIG_SERVICE_URL,
        timeout=_TIMEOUT,
        headers=service_headers(project_id),
    ) as client:
        resp = await client.get("/v1/admin/experiments", params={"project_id": project_id})
        resp.raise_for_status()
        data = resp.json()
        return data.get("experiments", []) if isinstance(data, dict) else data


_CONDITION_OPERATORS = {
    "equals",
    "not_equals",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "in",
    "not_in",
    "exists",
    "not_exists",
}
_VALUELESS_OPERATORS = {"exists", "not_exists"}


def _strict_condition(condition: Any) -> dict[str, Any]:
    """Validate one canonical condition without aliases or silent projection."""
    if not isinstance(condition, dict):
        raise ValueError("targeting conditions must be objects")
    attribute = condition.get("attribute")
    if not isinstance(attribute, str) or not attribute.strip():
        raise ValueError("targeting condition attribute must be a non-empty string")
    operator = condition.get("operator")
    if operator not in _CONDITION_OPERATORS:
        raise ValueError(f"unsupported targeting operator: {operator!r}")

    expected_keys = (
        {"attribute", "operator"}
        if operator in _VALUELESS_OPERATORS
        else {"attribute", "operator", "value"}
    )
    if set(condition) != expected_keys:
        raise ValueError(
            f"targeting operator {operator!r} requires exactly "
            f"{sorted(expected_keys)}"
        )
    if operator not in _VALUELESS_OPERATORS and condition["value"] is None:
        raise ValueError(f"targeting operator {operator!r} requires a non-null value")
    return dict(condition)


def _targeting_to_rules(targeting: dict[str, Any] | list | None) -> list[dict[str, Any]]:
    """Convert the one strict design shape into Config's canonical rule shape."""
    if targeting is None:
        return []
    if not isinstance(targeting, dict) or set(targeting) != {"conditions"}:
        raise ValueError("targeting must contain exactly one 'conditions' list")
    raw_conditions = targeting["conditions"]
    if not isinstance(raw_conditions, list):
        raise ValueError("targeting.conditions must be a list")
    conditions = [_strict_condition(condition) for condition in raw_conditions]
    if not conditions:
        return []
    return [
        {
            "id": "targeting",
            "conditions": conditions,
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }
    ]


async def create_experiment_config(
    project_id: str,
    experiment_id: str,
    hypothesis: str,
    variants: list[dict[str, Any]],
    default_variant: str,
    primary_metric: dict[str, str],
    secondary_metrics: list[dict[str, str]] | None = None,
    guardrail_metrics: list[dict[str, Any]] | None = None,
    targeting: dict[str, Any] | None = None,
    estimated_duration_days: int = 14,
    flag_key: str | None = None,
    traffic_percentage: float = 100.0,
) -> dict[str, Any]:
    """Create a running experiment via the Config service.

    The Config service owns experiment→flag initialization, so this single call
    also creates the canonical backing flag (keyed by ``flag_key``) that controls
    variant assignment — the agent no longer creates the flag separately.

    Args:
        project_id: Project scope.
        experiment_id: Unique experiment identifier (e.g. "exp_checkout_v2").
        hypothesis: The hypothesis being tested (stored as the description).
        variants: Variant definitions with "key", "weight", and optional "description".
        default_variant: Explicit declared control variant key.
        primary_metric: Dict with "event", "type", and "direction".
        secondary_metrics: Optional additional metrics to track.
        guardrail_metrics: Metrics that should not degrade.
        targeting: User targeting conditions.
        estimated_duration_days: Expected experiment runtime.
        flag_key: Backing flag key to use (defaults to experiment_id).
        traffic_percentage: Share of traffic the backing flag exposes to the
            experiment. Must be the same number the safety validator judged
            (the design's fallthrough rollout) — deploying at a hardcoded 100%
            would void the blast-radius check.

    Returns:
        The created experiment configuration.
    """
    if (
        isinstance(estimated_duration_days, bool)
        or not isinstance(estimated_duration_days, int)
        or not 1 <= estimated_duration_days <= 90
    ):
        raise ValueError("estimated_duration_days must be an integer from 1 to 90")
    if not isinstance(primary_metric, dict) or not primary_metric.get("event"):
        raise ValueError("primary_metric.event is required for a running experiment")
    if primary_metric.get("type", "conversion") != "conversion":
        raise ValueError("primary_metric.type must be conversion")
    if not 2 <= len(variants) <= 10:
        raise ValueError("variants must contain between 2 and 10 variants")
    if any(
        not isinstance(variant, dict)
        or isinstance(variant.get("weight"), bool)
        or not isinstance(variant.get("weight"), int)
        or variant["weight"] <= 0
        for variant in variants
    ):
        raise ValueError("experiment variant weights must be positive integers")
    variant_keys = [variant.get("key") for variant in variants]
    if not isinstance(default_variant, str) or default_variant not in variant_keys:
        raise ValueError("default_variant must match a variant key")

    start_date = datetime.now(UTC)
    end_date = start_date + timedelta(days=estimated_duration_days)
    payload: dict[str, Any] = {
        "key": experiment_id,
        "flag_key": flag_key or experiment_id,
        "status": "running",
        "description": hypothesis,
        "variants": variants,
        "default_variant": default_variant,
        "traffic_percentage": traffic_percentage,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "primary_metric": {
            "event": primary_metric["event"],
            "type": primary_metric.get("type", "conversion"),
            "direction": primary_metric.get("direction", "increase"),
        },
    }
    targeting_rules = _targeting_to_rules(targeting)
    if targeting_rules:
        payload["targeting_rules"] = targeting_rules

    async with httpx.AsyncClient(
        base_url=CONFIG_SERVICE_URL,
        timeout=_TIMEOUT,
        headers=service_headers(project_id),
    ) as client:
        resp = await client.post(
            "/v1/admin/experiments",
            json=payload,
            params={"project_id": project_id},
        )
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
    project_id: str,
) -> dict[str, Any]:
    """Get authoritative read-only results for an experiment.

    Args:
        experiment_id: The experiment to analyse.
        project_id: Project ID.

    Returns:
        Full experiment analysis results.
    """
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
    """Minimal sample-size calculator for use within the agents service.

    Stdlib only — scipy is not a dependency of this service, so the previous
    ``from scipy import stats`` raised ModuleNotFoundError on first use.
    """

    def calculate_sample_size(
        self, baseline_rate: float, mde: float,
        alpha: float = 0.05, power: float = 0.8,
    ) -> int:
        import math
        from statistics import NormalDist

        p1 = min(max(baseline_rate, 0.0), 1.0)
        # Clamp so baseline_rate + mde > 1 doesn't put a negative under sqrt.
        p2 = min(max(p1 + mde, 0.0), 1.0)
        p_bar = (p1 + p2) / 2.0
        z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
        z_beta = NormalDist().inv_cdf(power)
        num = (z_alpha * math.sqrt(2 * p_bar * (1 - p_bar))
               + z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
        return math.ceil(num / (mde ** 2)) if mde != 0 else 0
