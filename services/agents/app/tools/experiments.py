"""Experiment management tools — wrappers around Config and Query Service APIs."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.service_auth import service_headers

QUERY_SERVICE_URL = os.getenv("QUERY_SERVICE_URL", "http://localhost:8082")
CONFIG_SERVICE_URL = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
_TIMEOUT = 30.0
_STATISTICAL_PROTOCOL = "fixed_horizon_fisher_newcombe_cc_plan_v1"
_STATISTICAL_PLAN_FIELDS = {
    "protocol",
    "baseline_conversion_rate",
    "minimum_detectable_effect",
    "significance_level",
    "nominal_power",
    "required_sample_size_per_arm",
    "data_settlement_seconds",
}
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


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
        if not isinstance(data, dict) or set(data) != {"experiments", "count"}:
            raise ValueError(
                "Config experiments response must contain exactly experiments and count"
            )
        experiments = data["experiments"]
        count = data["count"]
        if (
            not isinstance(experiments, list)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count != len(experiments)
        ):
            raise ValueError("Config experiments response count does not match its list")
        return experiments


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
            "name": "",
            "conditions": conditions,
        }
    ]


def _strict_statistical_plan(plan: Any) -> dict[str, Any]:
    """Validate the immutable Config-owned fixed-horizon plan shape."""
    if not isinstance(plan, dict) or set(plan) != _STATISTICAL_PLAN_FIELDS:
        raise ValueError(
            "statistical_plan must contain exactly "
            f"{sorted(_STATISTICAL_PLAN_FIELDS)}"
        )
    if plan["protocol"] != _STATISTICAL_PROTOCOL:
        raise ValueError(f"statistical_plan.protocol must be {_STATISTICAL_PROTOCOL!r}")

    for field in (
        "baseline_conversion_rate",
        "minimum_detectable_effect",
        "significance_level",
        "nominal_power",
    ):
        value = plan[field]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"statistical_plan.{field} must be a number")
    if not 0 <= plan["baseline_conversion_rate"] <= 1:
        raise ValueError("statistical_plan.baseline_conversion_rate must be between 0 and 1")
    for field in ("minimum_detectable_effect", "significance_level", "nominal_power"):
        if not 0 < plan[field] < 1:
            raise ValueError(f"statistical_plan.{field} must be between 0 and 1")
    if not 1e-6 <= plan["minimum_detectable_effect"] <= 1:
        raise ValueError("statistical_plan.minimum_detectable_effect is out of bounds")
    if not 1e-6 <= plan["significance_level"] <= 0.5:
        raise ValueError("statistical_plan.significance_level is out of bounds")
    if not 0.5 < plan["nominal_power"] <= 0.9999:
        raise ValueError("statistical_plan.nominal_power is out of bounds")

    target = plan["required_sample_size_per_arm"]
    if (
        isinstance(target, bool)
        or not isinstance(target, int)
        or not 2 <= target <= 10_000_000
    ):
        raise ValueError(
            "statistical_plan.required_sample_size_per_arm must be an integer of at least 2"
        )
    settlement = plan["data_settlement_seconds"]
    if (
        isinstance(settlement, bool)
        or not isinstance(settlement, int)
        or not 1 <= settlement <= 86_400
    ):
        raise ValueError(
            "statistical_plan.data_settlement_seconds must be an integer from 1 to 86400"
        )
    return dict(plan)


async def create_experiment_draft(
    project_id: str,
    idempotency_key: str,
    experiment_id: str,
    hypothesis: str,
    variants: list[dict[str, Any]],
    default_variant: str,
    primary_metric: dict[str, str],
    statistical_plan: dict[str, Any],
    targeting: dict[str, Any] | None = None,
    flag_key: str | None = None,
    traffic_percentage: float = 100.0,
) -> dict[str, Any]:
    """Create an inert experiment draft via the Config service.

    The Config service owns experiment→flag initialization, so this single call
    also creates the canonical backing flag (keyed by ``flag_key``). Draft
    experiments create disabled draft flags and carry no lifecycle dates, so
    assignment cannot begin before the treatment changeset exists.

    Args:
        project_id: Project scope.
        experiment_id: Unique experiment identifier (e.g. "exp_checkout_v2").
        hypothesis: The hypothesis being tested (stored as the description).
        variants: Variant definitions with "key", "weight", and optional "description".
        default_variant: Explicit declared control variant key.
        primary_metric: Dict with "event", "type", and "direction".
        statistical_plan: Immutable fixed-horizon decision contract. Config
            validates its prospective sample target before accepting traffic.
        targeting: User targeting conditions.
        flag_key: Backing flag key to use (defaults to experiment_id).
        traffic_percentage: Proposed traffic share retained on the disabled
            backing flag. It must match the number the safety validator judged.

    Returns:
        The created experiment configuration.
    """
    if _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None:
        raise ValueError("idempotency_key must be a canonical 1 to 200 character key")
    if not isinstance(primary_metric, dict) or not primary_metric.get("event"):
        raise ValueError("primary_metric.event is required for an experiment draft")
    if primary_metric.get("type", "conversion") != "conversion":
        raise ValueError("primary_metric.type must be conversion")
    if primary_metric.get("direction", "increase") not in {"increase", "decrease"}:
        raise ValueError("primary_metric.direction must be increase or decrease")
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

    payload: dict[str, Any] = {
        "key": experiment_id,
        "flag_key": flag_key or experiment_id,
        "status": "draft",
        "description": hypothesis,
        "variants": variants,
        "default_variant": default_variant,
        "traffic_percentage": traffic_percentage,
        "primary_metric": {
            "event": primary_metric["event"],
            "type": primary_metric.get("type", "conversion"),
            "direction": primary_metric.get("direction", "increase"),
        },
        "statistical_plan": _strict_statistical_plan(statistical_plan),
    }
    targeting_rules = _targeting_to_rules(targeting)
    if targeting_rules:
        payload["targeting_rules"] = targeting_rules

    async with httpx.AsyncClient(
        base_url=CONFIG_SERVICE_URL,
        timeout=_TIMEOUT,
        headers={
            **service_headers(project_id),
            "Idempotency-Key": idempotency_key,
        },
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
    nominal_power: float = 0.8,
    treatment_count: int = 1,
    direction: str = "increase",
    data_settlement_seconds: int = 300,
) -> dict[str, Any]:
    """Build the canonical conservative fixed-horizon statistical plan.

    Args:
        baseline_rate: Expected conversion rate for control (e.g. 0.10 for 10%).
        minimum_detectable_effect: Smallest absolute effect to detect (e.g. 0.02 for 2pp).
        alpha: Significance level (default 0.05).
        nominal_power: Nominal planning power (default 0.8); this is not a
            guarantee of exact achieved Fisher power.
        treatment_count: Number of treatment-vs-control comparisons.
        direction: Primary metric direction (``increase`` or ``decrease``).
        data_settlement_seconds: Explicit post-horizon hold before a decision
            snapshot may be reported. It is not a writer-watermark guarantee.

    Returns:
        Canonical statistical plan accepted by Config.
    """
    if isinstance(treatment_count, bool) or not isinstance(treatment_count, int) or treatment_count < 1:
        raise ValueError("treatment_count must be a positive integer")
    if direction not in {"increase", "decrease"}:
        raise ValueError("direction must be increase or decrease")
    if not 1e-6 <= minimum_detectable_effect <= 1:
        raise ValueError("minimum_detectable_effect must be between 1e-6 and 1")
    if not 1e-6 <= alpha <= 0.5:
        raise ValueError("alpha must be between 1e-6 and 0.5")
    if not 0.5 < nominal_power <= 0.9999:
        raise ValueError("nominal_power must be greater than 0.5 and at most 0.9999")
    if (
        isinstance(data_settlement_seconds, bool)
        or not isinstance(data_settlement_seconds, int)
        or not 1 <= data_settlement_seconds <= 86_400
    ):
        raise ValueError("data_settlement_seconds must be an integer from 1 to 86400")
    signed_effect = minimum_detectable_effect if direction == "increase" else -minimum_detectable_effect
    treatment_rate = baseline_rate + signed_effect
    if not 0 <= baseline_rate <= 1 or not 0 <= treatment_rate <= 1:
        raise ValueError("minimum_detectable_effect is incompatible with baseline_rate and direction")

    n = _InlineAnalyzer().calculate_sample_size(
        baseline_rate,
        signed_effect,
        alpha / treatment_count,
        nominal_power,
    )
    plan = {
        "protocol": _STATISTICAL_PROTOCOL,
        "baseline_conversion_rate": baseline_rate,
        "minimum_detectable_effect": minimum_detectable_effect,
        "significance_level": alpha,
        "nominal_power": nominal_power,
        "required_sample_size_per_arm": n,
        "data_settlement_seconds": data_settlement_seconds,
    }
    return _strict_statistical_plan(plan)


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

        p1 = baseline_rate
        p2 = p1 + mde
        p_bar = (p1 + p2) / 2.0
        z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
        z_beta = NormalDist().inv_cdf(power)
        num = (z_alpha * math.sqrt(2 * p_bar * (1 - p_bar))
               + z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
        if mde == 0:
            return 0
        asymptotic_n = num / (mde**2)
        corrected_n = (
            asymptotic_n
            / 4.0
            * (1.0 + math.sqrt(1.0 + 4.0 / (asymptotic_n * abs(mde)))) ** 2
        )
        target = math.ceil(corrected_n)
        if target > 10_000_000:
            raise ValueError("statistical plan exceeds the supported per-arm target")
        return target
