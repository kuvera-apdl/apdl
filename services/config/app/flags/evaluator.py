"""Canonical feature gate evaluation engine."""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_UINT32_MAX = 0xFFFFFFFF


def hash_bucket(flag_key: str, salt: str, unit_id: str) -> int:
    """FNV-1a 32-bit hash for deterministic gate bucketing."""
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
    if attribute == "user_id":
        return True, ctx.get("user_id", "")
    if attribute == "anonymous_id":
        return True, ctx.get("anonymous_id", "")

    attributes = ctx.get("attributes", {})
    if isinstance(attributes, dict) and attribute in attributes:
        return True, attributes[attribute]
    return False, None


def matches_condition(condition: dict, ctx: dict) -> bool:
    """Check a canonical condition against an evaluation context."""
    if not isinstance(condition, dict):
        return False

    attribute = condition.get("attribute")
    operator = condition.get("operator")
    if not isinstance(attribute, str) or not isinstance(operator, str):
        return False

    exists, actual = _resolve_attribute(attribute, ctx)
    if operator == "exists":
        return exists and bool(actual)
    if operator == "not_exists":
        return not exists or not bool(actual)
    if not exists or "value" not in condition:
        return False

    expected = condition["value"]
    actual_value = str(actual)

    if operator == "equals":
        return actual_value == str(expected)
    if operator == "not_equals":
        return actual_value != str(expected)
    if operator == "contains":
        return isinstance(expected, str) and expected in actual_value
    if operator == "not_contains":
        return isinstance(expected, str) and expected not in actual_value
    if operator == "starts_with":
        return isinstance(expected, str) and actual_value.startswith(expected)
    if operator == "ends_with":
        return isinstance(expected, str) and actual_value.endswith(expected)
    if operator == "in":
        return isinstance(expected, list) and actual in expected
    if operator == "not_in":
        return not isinstance(expected, list) or actual not in expected
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            actual_number = float(actual)
            expected_number = float(expected)
        except (TypeError, ValueError):
            return False

        if operator == "gt":
            return actual_number > expected_number
        if operator == "gte":
            return actual_number >= expected_number
        if operator == "lt":
            return actual_number < expected_number
        return actual_number <= expected_number
    if operator == "regex":
        if not isinstance(expected, str):
            return False
        try:
            return bool(re.search(expected, actual_value))
        except re.error:
            logger.warning("Invalid regex in flag rule: %s", expected)
            return False

    logger.debug("Unknown operator '%s' in gate rule", operator)
    return False


def matches_rule(rule: dict, ctx: dict) -> bool:
    conditions = rule.get("conditions", [])
    if not isinstance(conditions, list):
        return False
    return all(matches_condition(condition, ctx) for condition in conditions)


def _unit_id(ctx: dict, bucket_by: str) -> str:
    exists, value = _resolve_attribute(bucket_by, ctx)
    if exists and value is not None:
        return str(value)
    return ""


def _base_result(flag: dict) -> dict:
    return {
        "key": flag.get("key", ""),
        "value": bool(flag.get("default_value", False)),
        "reason": "",
        "rule_id": "",
        "bucket": None,
        "rollout_percentage": None,
        "bucket_by": "",
        "config_version": int(flag.get("version", 0)),
    }


def _apply_rollout(
    flag: dict,
    rollout: dict,
    ctx: dict,
) -> tuple[bool, float | None, float, str]:
    percentage = float(rollout.get("percentage", 0.0))
    bucket_by = rollout.get("bucket_by", "user_id")
    unit_id = _unit_id(ctx, bucket_by)
    if not unit_id:
        return False, None, percentage, bucket_by
    bucket = percentage_bucket(flag.get("key", ""), flag.get("salt", ""), unit_id)
    return bucket < percentage, bucket, percentage, bucket_by


def evaluate(flag: dict, ctx: dict) -> dict:
    """Evaluate one canonical gate config against a context."""
    result = _base_result(flag)

    if not flag.get("enabled", False):
        result["reason"] = "disabled"
        return result

    for rule in flag.get("rules", []):
        if not isinstance(rule, dict) or not matches_rule(rule, ctx):
            continue

        passed, bucket, percentage, bucket_by = _apply_rollout(
            flag,
            rule.get("rollout", {}),
            ctx,
        )
        result["rule_id"] = rule.get("id", "")
        result["bucket"] = bucket
        result["rollout_percentage"] = percentage
        result["bucket_by"] = bucket_by
        if bucket is None:
            result["reason"] = "error"
            return result
        if passed:
            result["value"] = True
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
    result["bucket"] = bucket
    result["rollout_percentage"] = percentage
    result["bucket_by"] = bucket_by
    if bucket is None:
        result["reason"] = "error"
        return result
    if passed:
        result["value"] = bool(fallthrough.get("value", False))
        result["reason"] = "fallthrough"
    else:
        result["reason"] = "fallthrough_rollout"
    return result


def evaluate_all(flags: list[dict], ctx: dict) -> list[dict]:
    """Evaluate all canonical gates against a context."""
    return [evaluate(flag, ctx) for flag in flags]
