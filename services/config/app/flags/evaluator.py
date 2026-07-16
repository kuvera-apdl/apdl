"""Canonical feature flag evaluation engine."""

import logging
from typing import Any

from pydantic import ValidationError

from app.flags.targeting_contract import (
    MAX_CONDITIONS_PER_RULE,
    MAX_RULES,
    NUMERIC_OPERATORS,
    PRESENCE_OPERATORS,
    SUPPORTED_OPERATORS,
    is_bounded_string,
    is_condition_value_valid,
    is_identifier,
    is_membership_list,
    is_scalar,
    parse_numeric,
    scalar_equal,
)
from app.models.schemas import FallthroughConfig, RolloutConfig

logger = logging.getLogger(__name__)

_UINT32_MAX = 0xFFFFFFFF


def hash_bucket(flag_key: str, salt: str, unit_id: str) -> int:
    """FNV-1a 32-bit hash for deterministic flag bucketing."""
    data = f"{flag_key}:{salt}:{unit_id}"
    h = 2166136261
    for char in data.encode("utf-8"):
        h ^= char
        h = (h * 16777619) & _UINT32_MAX
    return h


def percentage_bucket(flag_key: str, salt: str, unit_id: str) -> float:
    return (hash_bucket(flag_key, salt, unit_id) / _UINT32_MAX) * 100.0


def is_in_rollout(flag_key: str, salt: str, unit_id: str, percentage: float) -> bool:
    """Check if an evaluation unit falls within a rollout percentage."""
    if percentage >= 100.0:
        return True
    if percentage <= 0.0:
        return False
    return percentage_bucket(flag_key, salt, unit_id) < percentage


def _resolve_attribute(attribute: str, ctx: dict) -> tuple[bool, Any]:
    # Presence contract (canonical, must stay byte-for-byte identical across the
    # config service, the JS SDK, and the Python SDK): an attribute is *present*
    # only when its resolved value is non-null. A null/None value — whether an
    # explicit ``user_id: null`` identity or a ``null`` trait — is treated as
    # ABSENT, exactly like a missing key. This is deliberate: a null is never
    # stringified into a value comparison, which is what keeps the three
    # evaluators in lockstep (otherwise a null would compare against ``str(None)``
    # = "None" here but ``String(null)`` = "null" in JS). Falsy non-null values
    # (``""``, ``0``, ``false``) remain present. See fixtures/gates/parity.json.
    if attribute == "user_id":
        value = ctx.get("user_id")
        return value is not None, value
    if attribute == "anonymous_id":
        value = ctx.get("anonymous_id")
        return value is not None, value

    attributes = ctx.get("attributes", {})
    if isinstance(attributes, dict):
        value = attributes.get(attribute)
        if value is not None:
            return True, value
    return False, None


def matches_condition(condition: dict, ctx: dict) -> bool:
    """Check a canonical condition against an evaluation context."""
    if not isinstance(condition, dict):
        return False

    attribute = condition.get("attribute")
    operator = condition.get("operator")
    if (
        not is_identifier(attribute)
        or not isinstance(operator, str)
        or operator not in SUPPORTED_OPERATORS
    ):
        return False

    has_value = "value" in condition
    if operator in PRESENCE_OPERATORS:
        if has_value:
            return False
    elif not has_value or not is_condition_value_valid(operator, condition["value"]):
        return False

    exists, actual = _resolve_attribute(attribute, ctx)
    if operator == "exists":
        return exists and actual is not None
    if operator == "not_exists":
        return not exists or actual is None
    if not exists:
        return False

    expected = condition["value"]

    if operator == "equals":
        return scalar_equal(actual, expected)
    if operator == "not_equals":
        return is_scalar(actual) and not scalar_equal(actual, expected)
    if operator == "contains":
        return is_bounded_string(actual) and expected in actual
    if operator == "not_contains":
        return is_bounded_string(actual) and expected not in actual
    if operator == "starts_with":
        return is_bounded_string(actual) and actual.startswith(expected)
    if operator == "ends_with":
        return is_bounded_string(actual) and actual.endswith(expected)
    if operator == "in":
        return is_scalar(actual) and is_membership_list(expected) and any(
            scalar_equal(actual, item) for item in expected
        )
    if operator == "not_in":
        return is_scalar(actual) and is_membership_list(expected) and not any(
            scalar_equal(actual, item) for item in expected
        )
    if operator in NUMERIC_OPERATORS:
        actual_number = parse_numeric(actual)
        expected_number = parse_numeric(expected)
        if actual_number is None or expected_number is None:
            return False

        if operator == "gt":
            return actual_number > expected_number
        if operator == "gte":
            return actual_number >= expected_number
        if operator == "lt":
            return actual_number < expected_number
        return actual_number <= expected_number
    logger.debug("Unknown operator '%s' in flag rule", operator)
    return False


def matches_rule(rule: dict, ctx: dict) -> bool:
    conditions = rule.get("conditions", [])
    if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS_PER_RULE:
        return False
    return all(matches_condition(condition, ctx) for condition in conditions)


def _unit_id(ctx: dict, bucket_by: str) -> str:
    if not is_identifier(bucket_by):
        return ""
    exists, value = _resolve_attribute(bucket_by, ctx)
    if exists and is_identifier(value):
        return value
    return ""


def _rules_within_limits(rules: Any) -> bool:
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        return False
    for rule in rules:
        if not isinstance(rule, dict) or not is_identifier(rule.get("id")):
            return False
        conditions = rule.get("conditions")
        if not isinstance(conditions, list) or len(conditions) > MAX_CONDITIONS_PER_RULE:
            return False
    return True


def _base_result(flag: dict) -> dict:
    key = flag.get("key")
    version = flag.get("version")
    return {
        "key": key if isinstance(key, str) else "",
        "variant": None,
        "reason": "",
        "rule_id": None,
        "rollout_bucket": None,
        "variant_bucket": None,
        "rollout_percentage": None,
        "bucket_by": None,
        "config_version": (
            version
            if isinstance(version, int) and not isinstance(version, bool) and version >= 1
            else None
        ),
        "source": None,
    }


def _canonical_evaluation_flag(flag: dict) -> dict:
    """Parse every durable rollout without coercing malformed values."""
    canonical = dict(flag)
    rules = flag.get("rules")
    if isinstance(rules, list):
        canonical["rules"] = [
            {
                **rule,
                "rollout": RolloutConfig.model_validate(
                    rule.get("rollout")
                ).model_dump(mode="python"),
            }
            if isinstance(rule, dict)
            else rule
            for rule in rules
        ]
    canonical["fallthrough"] = FallthroughConfig.model_validate(
        flag.get("fallthrough")
    ).model_dump(mode="python")
    return canonical


def _apply_rollout(
    flag: dict,
    rollout: dict,
    ctx: dict,
) -> tuple[bool, float | None, float, str]:
    percentage = rollout["percentage"]
    bucket_by = rollout["bucket_by"]
    unit_id = _unit_id(ctx, bucket_by)
    if not unit_id:
        return False, None, percentage, bucket_by
    bucket = percentage_bucket(flag.get("key", ""), f"{flag.get('salt', '')}:rollout", unit_id)
    return bucket < percentage, bucket, percentage, bucket_by


def assign_weighted_variant(variants: list[dict], variant_bucket: float) -> str | None:
    total_weight = sum(
        variant.get("weight", 0)
        for variant in variants
        if isinstance(variant, dict) and isinstance(variant.get("weight"), int)
    )
    if total_weight <= 0:
        return None

    target = (variant_bucket / 100.0) * total_weight
    cumulative = 0
    last_positive_variant: str | None = None
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        key = variant.get("key")
        weight = variant.get("weight")
        if not isinstance(key, str) or not isinstance(weight, int) or weight <= 0:
            continue
        last_positive_variant = key
        cumulative += weight
        if target < cumulative:
            return key

    return last_positive_variant


def _assign_variant(flag: dict, ctx: dict, bucket_by: str) -> tuple[str, float | None]:
    default_variant = flag.get("default_variant", "control")
    unit_id = _unit_id(ctx, bucket_by)
    if not unit_id:
        return default_variant, None
    variant_bucket = percentage_bucket(
        flag.get("key", ""),
        f"{flag.get('salt', '')}:variant",
        unit_id,
    )
    variant = assign_weighted_variant(flag.get("variants", []), variant_bucket)
    return variant or default_variant, variant_bucket


def evaluate(flag: dict, ctx: dict) -> dict:
    """Evaluate one canonical flag config against a context."""
    result = _base_result(flag)
    try:
        flag = _canonical_evaluation_flag(flag)
    except (ValidationError, TypeError, ValueError):
        result["reason"] = "invalid_config"
        return result
    result["variant"] = flag.get("default_variant", "control")

    # The SDK evaluators gate only on ``enabled`` and never look at ``state``.
    # That is safe — not a parity gap — because the flags table enforces
    # ``CHECK ((state = 'active') = enabled)`` (see main.py), so a non-active flag
    # is always ``enabled = false``. An "archived-but-enabled" flag is therefore
    # unrepresentable, and this server-side ``state`` check is a redundant guard,
    # not a behavior the clients must mirror. Both branches yield ``disabled``.
    if flag.get("state", "active") != "active":
        result["reason"] = "disabled"
        return result

    if not flag.get("enabled", False):
        result["reason"] = "disabled"
        return result

    rules = flag.get("rules", [])
    if not _rules_within_limits(rules):
        result["reason"] = "error"
        return result

    for rule in rules:
        if not isinstance(rule, dict) or not matches_rule(rule, ctx):
            continue

        passed, bucket, percentage, bucket_by = _apply_rollout(
            flag,
            rule.get("rollout", {}),
            ctx,
        )
        result["rule_id"] = rule.get("id", "")
        result["rollout_bucket"] = bucket
        result["rollout_percentage"] = percentage
        result["bucket_by"] = bucket_by
        if bucket is None:
            result["reason"] = "error"
            return result
        if passed:
            result["variant"], result["variant_bucket"] = _assign_variant(flag, ctx, bucket_by)
            result["reason"] = "rule_match"
        else:
            result["reason"] = "rule_rollout"
        return result

    fallthrough = flag.get("fallthrough", {})
    passed, bucket, percentage, bucket_by = _apply_rollout(
        flag,
        fallthrough.get("rollout", {}),
        ctx,
    )
    result["rollout_bucket"] = bucket
    result["rollout_percentage"] = percentage
    result["bucket_by"] = bucket_by
    if bucket is None:
        result["reason"] = "error"
        return result
    if passed:
        result["variant"], result["variant_bucket"] = _assign_variant(flag, ctx, bucket_by)
        result["reason"] = "fallthrough"
    else:
        result["reason"] = "fallthrough_rollout"
    return result


def evaluate_all(flags: list[dict], ctx: dict) -> list[dict]:
    """Evaluate all canonical flags against a context."""
    return [evaluate(flag, ctx) for flag in flags]
