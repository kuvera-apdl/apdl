"""Canonical local variant feature flag evaluator.

Ported from ``services/config/app/flags/evaluator.py`` (condition semantics,
namespaced bucketing, weighted variant assignment) and
``sdk/javascript/src/flags/evaluator.ts`` (result shape, ``source``/``reason``
fields, invalid-config handling). Evaluating a flag here yields the same variant
the config service would return for the same context — the shared
``fixtures/gates/parity.json`` pins this.
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
    VariantConfig,
)

logger = logging.getLogger("apdl")

_NUMERIC_OPS = {
    ConditionOperator.GT,
    ConditionOperator.GTE,
    ConditionOperator.LT,
    ConditionOperator.LTE,
}


def assign_weighted_variant(
    variants: list[VariantConfig], variant_bucket: float
) -> str | None:
    """Map a ``0..100`` variant bucket onto relative integer weights.

    Returns the key of the first variant whose cumulative weight interval
    contains ``(variant_bucket / 100) * total_weight``, clamping to the last
    positive-weight variant at the upper boundary. Zero-weight variants are
    never assigned. Returns ``None`` when the total weight is not positive.
    """
    total_weight = sum(variant.weight for variant in variants)
    if total_weight <= 0:
        return None

    target = (variant_bucket / 100.0) * total_weight
    cumulative = 0
    last_positive_variant: str | None = None
    for variant in variants:
        if variant.weight <= 0:
            continue
        last_positive_variant = variant.key
        cumulative += variant.weight
        if target < cumulative:
            return variant.key

    return last_positive_variant


class FlagEvaluator:
    def __init__(self, cache: FlagCache) -> None:
        self._cache = cache

    def evaluate(self, key: str, context: EvalContext) -> GateEvaluationResult:
        flag = self._cache.get(key)

        if flag is None:
            if self._cache.is_invalid(key):
                return GateEvaluationResult(
                    key=key,
                    variant=None,
                    reason="invalid_config",
                    source=self._cache.get_invalid_source(key),
                )
            return GateEvaluationResult(
                key=key, variant=None, reason="not_found", source=None
            )

        source = self._cache.get_source(flag.key)
        default = flag.default_variant

        if not flag.enabled:
            return GateEvaluationResult(
                key=flag.key,
                variant=default,
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
                    variant=default,
                    reason="error",
                    rule_id=rule.id,
                    rollout_percentage=percentage,
                    bucket_by=bucket_by,
                    config_version=flag.version,
                    source=source,
                )
            if passed:
                variant, variant_bucket = self._assign_variant(flag, context, bucket_by)
                return GateEvaluationResult(
                    key=flag.key,
                    variant=variant,
                    reason="rule_match",
                    rule_id=rule.id,
                    rollout_bucket=bucket,
                    variant_bucket=variant_bucket,
                    rollout_percentage=percentage,
                    bucket_by=bucket_by,
                    config_version=flag.version,
                    source=source,
                )
            return GateEvaluationResult(
                key=flag.key,
                variant=default,
                reason="rule_rollout",
                rule_id=rule.id,
                rollout_bucket=bucket,
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
                variant=default,
                reason="error",
                rollout_percentage=percentage,
                bucket_by=bucket_by,
                config_version=flag.version,
                source=source,
            )
        if passed:
            variant, variant_bucket = self._assign_variant(flag, context, bucket_by)
            return GateEvaluationResult(
                key=flag.key,
                variant=variant,
                reason="fallthrough",
                rollout_bucket=bucket,
                variant_bucket=variant_bucket,
                rollout_percentage=percentage,
                bucket_by=bucket_by,
                config_version=flag.version,
                source=source,
            )
        return GateEvaluationResult(
            key=flag.key,
            variant=default,
            reason="fallthrough_rollout",
            rollout_bucket=bucket,
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

        # Presence is "resolved and non-null"; "", 0, and False are present.
        if op is ConditionOperator.EXISTS:
            return exists and actual is not None
        if op is ConditionOperator.NOT_EXISTS:
            return not exists or actual is None
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

    # ── Rollout / variant assignment ──────────────────────────────

    def _apply_rollout(
        self, flag: GateConfig, rollout: RolloutConfig, ctx: EvalContext
    ) -> tuple[bool, float | None, float, str]:
        unit_id = self._unit_id(ctx, rollout.bucket_by)
        if not unit_id:
            return False, None, rollout.percentage, rollout.bucket_by
        bucket = percentage_bucket(flag.key, f"{flag.salt}:rollout", unit_id)
        return bucket < rollout.percentage, bucket, rollout.percentage, rollout.bucket_by

    def _assign_variant(
        self, flag: GateConfig, ctx: EvalContext, bucket_by: str
    ) -> tuple[str, float | None]:
        unit_id = self._unit_id(ctx, bucket_by)
        if not unit_id:
            return flag.default_variant, None
        variant_bucket = percentage_bucket(flag.key, f"{flag.salt}:variant", unit_id)
        variant = assign_weighted_variant(flag.variants, variant_bucket)
        return (variant or flag.default_variant), variant_bucket

    def _unit_id(self, ctx: EvalContext, bucket_by: str) -> str:
        exists, value = self._resolve_attribute(bucket_by, ctx)
        if exists and value is not None:
            return str(value)
        return ""

    @staticmethod
    def _resolve_attribute(attribute: str, ctx: EvalContext) -> tuple[bool, Any]:
        # Identity that the caller did not provide (None) is absent, not "".
        if attribute == "user_id":
            if ctx.user_id is not None:
                return True, ctx.user_id
            return False, None
        if attribute == "anonymous_id":
            if ctx.anonymous_id is not None:
                return True, ctx.anonymous_id
            return False, None
        if attribute in ctx.attributes:
            return True, ctx.attributes[attribute]
        return False, None
