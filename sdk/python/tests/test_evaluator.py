"""Evaluator semantics — must match the config service and JS SDK."""

from __future__ import annotations

import pytest
from conftest import make_gate, make_rule

from apdl.flags.cache import FlagCache
from apdl.flags.evaluator import FlagEvaluator
from apdl.flags.models import (
    ConditionOperator,
    EvalContext,
    FallthroughConfig,
    GateCondition,
    RolloutConfig,
)


def evaluator_with(*gates):
    cache = FlagCache()
    cache.set(list(gates), "memory")
    return FlagEvaluator(cache)


def ctx(user_id="u1", **attrs) -> EvalContext:
    return EvalContext(user_id=user_id, attributes=attrs)


def test_not_found():
    result = FlagEvaluator(FlagCache()).evaluate("missing", ctx())
    assert result.value is False
    assert result.reason == "not_found"
    assert result.source == "none"


def test_invalid_config():
    cache = FlagCache()
    cache.mark_invalid(["bad"], "initial_fetch")
    result = FlagEvaluator(cache).evaluate("bad", ctx())
    assert result.reason == "invalid_config"
    assert result.source == "initial_fetch"


def test_disabled_returns_default():
    ev = evaluator_with(make_gate("g", enabled=False, default_value=True))
    result = ev.evaluate("g", ctx())
    assert result.reason == "disabled"
    assert result.value is True


def test_fallthrough_pass():
    ev = evaluator_with(make_gate("g"))  # fallthrough 100% -> value True
    result = ev.evaluate("g", ctx())
    assert result.reason == "fallthrough"
    assert result.value is True


def test_fallthrough_rollout_excluded():
    gate = make_gate(
        "g",
        default_value=False,
        fallthrough=FallthroughConfig(
            value=True, rollout=RolloutConfig(percentage=0.0, bucket_by="user_id")
        ),
    )
    result = evaluator_with(gate).evaluate("g", ctx())
    assert result.reason == "fallthrough_rollout"
    assert result.value is False


def test_rule_match():
    rule = make_rule([
        GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro")
    ])
    ev = evaluator_with(make_gate("g", rules=[rule]))
    assert ev.evaluate("g", ctx(plan="pro")).reason == "rule_match"
    # Non-matching rule falls through.
    assert ev.evaluate("g", ctx(plan="free")).reason == "fallthrough"


def test_rule_rollout_zero_excludes():
    rule = make_rule(
        [GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro")],
        percentage=0.0,
    )
    ev = evaluator_with(make_gate("g", default_value=False, rules=[rule]))
    result = ev.evaluate("g", ctx(plan="pro"))
    assert result.reason == "rule_rollout"
    assert result.value is False


def test_rollout_error_when_bucket_unit_missing():
    # bucket_by points at an attribute that does not resolve -> empty unit id.
    gate = make_gate(
        "g",
        fallthrough=FallthroughConfig(
            value=True, rollout=RolloutConfig(percentage=50.0, bucket_by="missing_attr")
        ),
    )
    result = evaluator_with(gate).evaluate("g", EvalContext(user_id=None))
    assert result.reason == "error"
    assert result.bucket is None


@pytest.mark.parametrize(
    ("operator", "value", "attr", "expected"),
    [
        (ConditionOperator.EQUALS, "pro", "pro", True),
        (ConditionOperator.NOT_EQUALS, "pro", "free", True),
        (ConditionOperator.CONTAINS, "ro", "pro", True),
        (ConditionOperator.NOT_CONTAINS, "x", "pro", True),
        (ConditionOperator.STARTS_WITH, "pr", "pro", True),
        (ConditionOperator.ENDS_WITH, "ro", "pro", True),
        (ConditionOperator.GT, 10, "11", True),
        (ConditionOperator.GTE, 10, "10", True),
        (ConditionOperator.LT, 10, "9", True),
        (ConditionOperator.LTE, 10, "10", True),
        (ConditionOperator.GT, 10, "not-a-number", False),
        (ConditionOperator.IN, ["a", "b"], "b", True),
        (ConditionOperator.NOT_IN, ["a", "b"], "c", True),
        (ConditionOperator.REGEX, r"^p.o$", "pro", True),
        (ConditionOperator.REGEX, "[", "pro", False),  # invalid regex -> False
    ],
)
def test_operators(operator, value, attr, expected):
    rule = make_rule([GateCondition(attribute="x", operator=operator, value=value)])
    ev = evaluator_with(make_gate("g", rules=[rule]))
    matched = ev.evaluate("g", ctx(x=attr)).reason == "rule_match"
    assert matched is expected


def test_exists_and_not_exists():
    exists_rule = make_rule(
        [GateCondition(attribute="x", operator=ConditionOperator.EXISTS)], rule_id="e"
    )
    ev = evaluator_with(make_gate("g", rules=[exists_rule]))
    assert ev.evaluate("g", ctx(x="yes")).reason == "rule_match"
    assert ev.evaluate("g", ctx()).reason == "fallthrough"  # x missing -> rule skipped


def test_all_conditions_must_match():
    rule = make_rule([
        GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro"),
        GateCondition(attribute="country", operator=ConditionOperator.EQUALS, value="US"),
    ])
    ev = evaluator_with(make_gate("g", rules=[rule]))
    assert ev.evaluate("g", ctx(plan="pro", country="US")).reason == "rule_match"
    assert ev.evaluate("g", ctx(plan="pro", country="CA")).reason == "fallthrough"


def test_user_id_is_a_resolvable_attribute():
    rule = make_rule([
        GateCondition(attribute="user_id", operator=ConditionOperator.EQUALS, value="vip")
    ])
    ev = evaluator_with(make_gate("g", rules=[rule]))
    assert ev.evaluate("g", EvalContext(user_id="vip")).reason == "rule_match"
