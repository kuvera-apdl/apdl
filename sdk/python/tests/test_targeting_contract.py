"""Shared strict targeting contract for the published Python evaluator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apdl.flags.cache import FlagCache
from apdl.flags.evaluator import FlagEvaluator
from apdl.flags.models import (
    ConditionOperator,
    EvalContext,
    FallthroughConfig,
    GateCondition,
    GateConfig,
    GateRule,
    RolloutConfig,
    VariantConfig,
)
from apdl.flags.targeting_contract import (
    MAX_CONDITIONS_PER_RULE,
    MAX_IDENTIFIER_LENGTH,
    MAX_MEMBERSHIP_VALUES,
    MAX_RULES,
    MAX_STRING_LENGTH,
    NUMERIC_PATTERN,
)

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "gates" / "targeting.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_flag(
    *,
    condition: GateCondition | None = None,
    bucket_by: str = "anonymous_id",
) -> GateConfig:
    rules: list[GateRule] = []
    if condition is not None:
        rules = [
            GateRule(
                id="fixture-rule",
                conditions=[condition],
                rollout=RolloutConfig(
                    percentage=100.0,
                    bucket_by="anonymous_id",
                ),
            )
        ]
    return GateConfig(
        key="targeting-fixture",
        enabled=True,
        default_variant="control",
        variants=[VariantConfig(key="control", weight=1)],
        salt="fixture-salt",
        rules=rules,
        fallthrough=FallthroughConfig(
            rollout=RolloutConfig(
                percentage=0.0 if condition is not None else 100.0,
                bucket_by=bucket_by,
            )
        ),
        version=1,
    )


def evaluate(flag: GateConfig, context: EvalContext):
    cache = FlagCache()
    cache.set([flag], "memory")
    return FlagEvaluator(cache).evaluate(flag.key, context)


def test_fixture_declares_the_runtime_contract() -> None:
    assert FIXTURE["fixture_schema_version"] == 1
    assert FIXTURE["numeric_pattern"] == NUMERIC_PATTERN
    assert FIXTURE["limits"] == {
        "max_rules": MAX_RULES,
        "max_conditions_per_rule": MAX_CONDITIONS_PER_RULE,
        "max_identifier_length": MAX_IDENTIFIER_LENGTH,
        "max_string_length": MAX_STRING_LENGTH,
        "max_membership_values": MAX_MEMBERSHIP_VALUES,
    }
    fixture_operators = {
        case["condition"]["operator"] for case in FIXTURE["condition_cases"]
    }
    assert fixture_operators == {operator.value for operator in ConditionOperator}


def test_shared_condition_cases_match_exactly() -> None:
    for case in FIXTURE["condition_cases"]:
        condition = GateCondition.model_validate(case["condition"])
        context = EvalContext.model_validate(case["context"])
        result = evaluate(make_flag(condition=condition), context)
        expected_reason = "rule_match" if case["expected_match"] else "fallthrough_rollout"

        assert result.reason == expected_reason, case["name"]


def test_shared_invalid_conditions_are_rejected() -> None:
    for case in FIXTURE["invalid_condition_cases"]:
        with pytest.raises(ValidationError):
            GateCondition.model_validate(case["condition"])


def test_shared_bucket_unit_cases_match_exactly() -> None:
    for case in FIXTURE["unit_cases"]:
        context = EvalContext.model_validate(case["context"])
        result = evaluate(make_flag(bucket_by=case["bucket_by"]), context)
        if case["expected_available"]:
            assert result.reason == "fallthrough", case["name"]
            assert result.rollout_bucket is not None, case["name"]
            assert result.variant_bucket is not None, case["name"]
        else:
            assert result.reason == "error", case["name"]
            assert result.rollout_bucket is None, case["name"]
            assert result.variant_bucket is None, case["name"]


def test_models_reject_rule_and_condition_limit_overflow() -> None:
    rule = GateRule(
        id="fixture-rule",
        conditions=[],
        rollout=RolloutConfig(percentage=100.0, bucket_by="anonymous_id"),
    )
    with pytest.raises(ValidationError):
        GateConfig(
            key="too-many-rules",
            enabled=True,
            default_variant="control",
            variants=[VariantConfig(key="control", weight=1)],
            salt="fixture-salt",
            rules=[rule] * (MAX_RULES + 1),
            fallthrough=FallthroughConfig(
                rollout=RolloutConfig(percentage=100.0, bucket_by="anonymous_id")
            ),
            version=1,
        )

    condition = GateCondition(attribute="value", operator="exists")
    with pytest.raises(ValidationError):
        GateRule(
            id="too-many-conditions",
            conditions=[condition] * (MAX_CONDITIONS_PER_RULE + 1),
            rollout=RolloutConfig(percentage=100.0, bucket_by="anonymous_id"),
        )


def test_models_reject_identifier_string_and_membership_limit_overflow() -> None:
    with pytest.raises(ValidationError):
        GateCondition(attribute="a" * (MAX_IDENTIFIER_LENGTH + 1), operator="exists")
    with pytest.raises(ValidationError):
        GateCondition(
            attribute="value",
            operator="equals",
            value="x" * (MAX_STRING_LENGTH + 1),
        )
    with pytest.raises(ValidationError):
        GateCondition(
            attribute="value",
            operator="in",
            value=["x"] * (MAX_MEMBERSHIP_VALUES + 1),
        )
    with pytest.raises(ValidationError):
        EvalContext(attributes={"a" * (MAX_IDENTIFIER_LENGTH + 1): "x"})
    with pytest.raises(ValidationError):
        EvalContext(attributes={"value": "x" * (MAX_STRING_LENGTH + 1)})
