"""Experiment management tools — wrappers around Config and Query Service APIs."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

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
        resp = await client.get("/v1/admin/experiments", params={"project_id": project_id})
        resp.raise_for_status()
        data = resp.json()
        return data.get("experiments", []) if isinstance(data, dict) else data


# Maps loose LLM operators onto the Config service's strict ConditionOperator
# set. Canonical operators map to themselves; anything not here is dropped.
_CONDITION_OPERATOR_ALIASES = {
    "equals": "equals", "not_equals": "not_equals", "gt": "gt", "gte": "gte",
    "lt": "lt", "lte": "lte", "contains": "contains", "not_contains": "not_contains",
    "starts_with": "starts_with", "ends_with": "ends_with", "regex": "regex",
    "in": "in", "not_in": "not_in", "exists": "exists", "not_exists": "not_exists",
    # common model aliases
    "eq": "equals", "==": "equals", "equal": "equals",
    "ne": "not_equals", "neq": "not_equals", "!=": "not_equals", "not_equal": "not_equals",
    "greater_than": "gt", "greater": "gt",
    "greater_than_or_equal": "gte", "greater_equal": "gte",
    "less_than": "lt", "less": "lt", "less_than_or_equal": "lte", "less_equal": "lte",
    "startswith": "starts_with", "endswith": "ends_with", "matches": "regex",
    "is_null": "not_exists", "isnull": "not_exists", "null": "not_exists",
    "is_absent": "not_exists", "absent": "not_exists",
    "is_not_null": "exists", "isnotnull": "exists", "not_null": "exists",
    "is_present": "exists", "present": "exists",
}
_VALUELESS_OPERATORS = {"exists", "not_exists"}


def _canonical_condition(condition: Any) -> dict[str, Any] | None:
    """Project one loose LLM condition onto the strict ``GateCondition`` shape.

    Keeps only ``attribute``/``operator``/``value`` (the model often adds a
    ``description``, which the strict schema rejects), maps the operator onto the
    canonical set, and drops the condition when the operator is unknown or a
    value-taking operator has no value. ``exists``/``not_exists`` carry no value.
    """
    if not isinstance(condition, dict):
        return None
    attribute = condition.get("attribute") or condition.get("property") or condition.get("field")
    if not isinstance(attribute, str) or not attribute.strip():
        return None
    operator = _CONDITION_OPERATOR_ALIASES.get(str(condition.get("operator", "equals")).strip().lower())
    if operator is None:
        return None
    clean: dict[str, Any] = {"attribute": attribute.strip(), "operator": operator}
    if operator not in _VALUELESS_OPERATORS:
        value = condition.get("value")
        if value is None:
            return None
        clean["value"] = value
    return clean


def _targeting_to_rules(targeting: dict[str, Any] | list | None) -> list[dict[str, Any]]:
    """Canonicalize loose targeting conditions into the flag's GateRule shape.

    The experiment-design output expresses targeting as a list of conditions;
    the Config experiment schema requires canonical ``GateRule`` objects (each
    with an ``id`` and a ``rollout``) whose conditions pass the strict
    ``GateCondition`` validator. Each condition is projected onto that shape
    (extra fields stripped, operator aliases mapped); conditions that can't be
    canonicalized are dropped. Matching users are fully included; variant split
    is by weight.
    """
    if isinstance(targeting, dict):
        raw_conditions = targeting.get("conditions", [])
    elif isinstance(targeting, list):
        raw_conditions = targeting
    else:
        raw_conditions = []
    conditions = [c for c in (_canonical_condition(rc) for rc in raw_conditions) if c is not None]
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
    payload: dict[str, Any] = {
        "key": experiment_id,
        "flag_key": flag_key or experiment_id,
        "status": "running",
        "description": hypothesis,
        "variants": variants,
        "traffic_percentage": traffic_percentage,
    }
    if isinstance(primary_metric, dict) and primary_metric.get("event"):
        payload["primary_metric"] = {
            "event": primary_metric["event"],
            "type": primary_metric.get("type", "conversion"),
            "direction": primary_metric.get("direction", "increase"),
        }
    targeting_rules = _targeting_to_rules(targeting)
    if targeting_rules:
        payload["targeting_rules"] = targeting_rules

    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post(
            "/v1/admin/experiments",
            json=payload,
            params={"project_id": project_id},
        )
        resp.raise_for_status()
        return resp.json()


async def update_experiment_status(
    project_id: str, experiment_id: str, status: str
) -> dict[str, Any]:
    """Transition an experiment's lifecycle status via the Config service.

    Allowed transitions are enforced server-side (running → completed|stopped;
    both terminal). Used by the evaluation agent to conclude experiments.
    """
    async with httpx.AsyncClient(base_url=CONFIG_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.put(
            f"/v1/admin/experiments/{quote(experiment_id, safe='')}",
            json={"status": status},
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
    metric: str,
    project_id: str,
    method: str = "frequentist",
    flag_key: str | None = None,
) -> dict[str, Any]:
    """Get statistical results for a running or completed experiment.

    Args:
        experiment_id: The experiment to analyse.
        metric: The conversion/metric event to evaluate.
        project_id: Project ID.
        method: Statistical method — "frequentist", "bayesian", or "sequential".
        flag_key: Feature flag key whose canonical variant exposures back this
            experiment. The query endpoint now requires it; defaults to
            ``experiment_id``, matching ``create_experiment_config`` which keys
            the underlying flag on the experiment id.

    Returns:
        Full experiment analysis results.
    """
    async with httpx.AsyncClient(base_url=QUERY_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"/v1/query/experiment/{quote(experiment_id, safe='')}",
            params={
                "metric": metric,
                "method": method,
                "project_id": project_id,
                "flag_key": flag_key or experiment_id,
            },
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
