"""Canonical local feature gate evaluator.

Ported from ``services/config/app/flags/evaluator.py`` (condition semantics) and
``sdk/javascript/src/flags/evaluator.ts`` (result shape, ``source``/``reason``
fields, invalid-config handling). Evaluating a gate here yields the same value
the config service would return for the same context.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .cache import FlagCache
from .hash import percentage_bucket
from .models import (
    ConditionOperator,
    EvalContext,
    GateCondition,
    GateConfig,
    GateEvaluationResult,
    RolloutConfig,
)

logger = logging.getLogger("apdl")

_NUMERIC_OPS = {
    ConditionOperator.GT,
    ConditionOperator.GTE,
    ConditionOperator.LT,
    ConditionOperator.LTE,
}


class FlagEvaluator:
    def __init__(self, cache: FlagCache) -> None:
        self._cache = cache

    def evaluate(self, key: str, context: EvalContext) -> GateEvaluationResult:
        flag = self._cache.get(key)

        if flag is None:
            if self._cache.is_invalid(key):
                return GateEvaluationResult(
                    key=key,
                    value=False,
                    reason="invalid_config",
                    source=self._cache.get_invalid_source(key),
                )
            return GateEvaluationResult(
                key=key, value=False, reason="not_found", source="none"
            )

        source = self._cache.get_source(flag.key)

        if not flag.enabled:
            return GateEvaluationResult(
                key=flag.key,
                value=flag.default_value,
                reason="disabled",
                config_version=flag.version,
                source=source,
            )

        for rule in flag.rules:
            if not self._matches_rule(rule.conditions, context):
                continue

            passed, bucket, percentage, bucket_by = self._apply_rollout(
                flag, rule.rollout, context
            )
            if bucket is None:
                return GateEvaluationResult(
                    key=flag.key,
                    value=flag.default_value,
                    reason="error",
                    rule_id=rule.id,
                    rollout_percentage=percentage,
                    bucket_by=bucket_by,
                    config_version=flag.version,
                    source=source,
                )
            return GateEvaluationResult(
                key=flag.key,
                value=True if passed else flag.default_value,
                reason="rule_match" if passed else "rule_rollout",
                rule_id=rule.id,
                bucket=bucket,
                rollout_percentage=percentage,
                bucket_by=bucket_by,
                config_version=flag.version,
                source=source,
            )

        passed, bucket, percentage, bucket_by = self._apply_rollout(
            flag, flag.fallthrough.rollout, context
        )
        if bucket is None:
            return GateEvaluationResult(
                key=flag.key,
                value=flag.default_value,
                reason="error",
                rollout_percentage=percentage,
                bucket_by=bucket_by,
                config_version=flag.version,
                source=source,
            )
        return GateEvaluationResult(
            key=flag.key,
            value=flag.fallthrough.value if passed else flag.default_value,
            reason="fallthrough" if passed else "fallthrough_rollout",
            bucket=bucket,
            rollout_percentage=percentage,
            bucket_by=bucket_by,
            config_version=flag.version,
            source=source,
        )

    # ── Rule / condition matching ─────────────────────────────────

    def _matches_rule(self, conditions: list[GateCondition], ctx: EvalContext) -> bool:
        return all(self._matches_condition(c, ctx) for c in conditions)

    def _matches_condition(self, condition: GateCondition, ctx: EvalContext) -> bool:
        exists, actual = self._resolve_attribute(condition.attribute, ctx)
        op = condition.operator

        if op is ConditionOperator.EXISTS:
            return exists and bool(actual)
        if op is ConditionOperator.NOT_EXISTS:
            return not exists or not bool(actual)
        if not exists:
            return False

        expected = condition.value
        actual_value = str(actual)

        if op is ConditionOperator.EQUALS:
            return actual_value == str(expected)
        if op is ConditionOperator.NOT_EQUALS:
            return actual_value != str(expected)
        if op is ConditionOperator.CONTAINS:
            return isinstance(expected, str) and expected in actual_value
        if op is ConditionOperator.NOT_CONTAINS:
            return isinstance(expected, str) and expected not in actual_value
        if op is ConditionOperator.STARTS_WITH:
            return isinstance(expected, str) and actual_value.startswith(expected)
        if op is ConditionOperator.ENDS_WITH:
            return isinstance(expected, str) and actual_value.endswith(expected)
        if op is ConditionOperator.IN:
            return isinstance(expected, list) and actual in expected
        if op is ConditionOperator.NOT_IN:
            return not isinstance(expected, list) or actual not in expected
        if op in _NUMERIC_OPS:
            return self._compare_numeric(op, actual, expected)
        if op is ConditionOperator.REGEX:
            if not isinstance(expected, str):
                return False
            try:
                return bool(re.search(expected, actual_value))
            except re.error:
                logger.warning("Invalid regex in flag rule: %s", expected)
                return False

        return False

    @staticmethod
    def _compare_numeric(op: ConditionOperator, actual: Any, expected: Any) -> bool:
        try:
            a = float(actual)
            b = float(expected)
        except (TypeError, ValueError):
            return False
        if op is ConditionOperator.GT:
            return a > b
        if op is ConditionOperator.GTE:
            return a >= b
        if op is ConditionOperator.LT:
            return a < b
        return a <= b

    # ── Rollout ───────────────────────────────────────────────────

    def _apply_rollout(
        self, flag: GateConfig, rollout: RolloutConfig, ctx: EvalContext
    ) -> tuple[bool, float | None, float, str]:
        unit_id = self._unit_id(ctx, rollout.bucket_by)
        if not unit_id:
            return False, None, rollout.percentage, rollout.bucket_by
        bucket = percentage_bucket(flag.key, flag.salt, unit_id)
        return bucket < rollout.percentage, bucket, rollout.percentage, rollout.bucket_by

    def _unit_id(self, ctx: EvalContext, bucket_by: str) -> str:
        exists, value = self._resolve_attribute(bucket_by, ctx)
        if exists and value is not None:
            return str(value)
        return ""

    @staticmethod
    def _resolve_attribute(attribute: str, ctx: EvalContext) -> tuple[bool, Any]:
        if attribute == "user_id":
            return True, ctx.user_id or ""
        if attribute == "anonymous_id":
            return True, ctx.anonymous_id or ""
        if attribute in ctx.attributes:
            return True, ctx.attributes[attribute]
        return False, None
