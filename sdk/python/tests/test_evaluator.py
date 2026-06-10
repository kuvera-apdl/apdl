"""Evaluator semantics — must match the config service and JS SDK."""

from __future__ import annotations

import pytest
from conftest import make_flag, make_rule

from apdl.flags.cache import FlagCache
from apdl.flags.evaluator import FlagEvaluator
from apdl.flags.models import (
    ConditionOperator,
    EvalContext,
    FallthroughConfig,
    GateCondition,
    RolloutConfig,
    VariantConfig,
)


def evaluator_with(*flags):
    cache = FlagCache()
    cache.set(list(flags), "memory")
    return FlagEvaluator(cache)


def ctx(user_id="u1", **attrs) -> EvalContext:
    return EvalContext(user_id=user_id, attributes=attrs)


def test_not_found():
    result = FlagEvaluator(FlagCache()).evaluate("missing", ctx())
    assert result.variant is None
    assert result.reason == "not_found"
    assert result.source is None


def test_invalid_config():
    cache = FlagCache()
    cache.mark_invalid(["bad"], "initial_fetch")
    result = FlagEvaluator(cache).evaluate("bad", ctx())
    assert result.variant is None
    assert result.reason == "invalid_config"
    assert result.source == "initial_fetch"


def test_disabled_returns_default_variant():
    ev = evaluator_with(make_flag("g", enabled=False, default_variant="treatment"))
    result = ev.evaluate("g", ctx())
    assert result.reason == "disabled"
    assert result.variant == "treatment"
    assert result.config_version == 1


def test_fallthrough_pass_assigns_variant():
    ev = evaluator_with(make_flag("g"))  # fallthrough 100% -> assignment
    result = ev.evaluate("g", ctx())
    assert result.reason == "fallthrough"
    assert result.variant in {"control", "treatment"}
    assert result.rollout_bucket is not None
    assert result.variant_bucket is not None


def test_fallthrough_rollout_excluded_returns_default():
    flag = make_flag(
        "g",
        default_variant="control",
        fallthrough=FallthroughConfig(
            rollout=RolloutConfig(percentage=0.0, bucket_by="user_id")
        ),
    )
    result = evaluator_with(flag).evaluate("g", ctx())
    assert result.reason == "fallthrough_rollout"
    assert result.variant == "control"
    assert result.variant_bucket is None


def test_rule_match_assigns_variant():
    rule = make_rule(
        [GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro")]
    )
    ev = evaluator_with(make_flag("g", rules=[rule]))
    matched = ev.evaluate("g", ctx(plan="pro"))
    assert matched.reason == "rule_match"
    assert matched.rule_id == "r1"
    assert matched.variant in {"control", "treatment"}
    # Non-matching rule falls through.
    assert ev.evaluate("g", ctx(plan="free")).reason == "fallthrough"


def test_rule_rollout_zero_excludes():
    rule = make_rule(
        [GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro")],
        percentage=0.0,
    )
    ev = evaluator_with(make_flag("g", default_variant="control", rules=[rule]))
    result = ev.evaluate("g", ctx(plan="pro"))
    assert result.reason == "rule_rollout"
    assert result.variant == "control"
    assert result.rule_id == "r1"


def test_rollout_error_when_bucket_unit_missing():
    flag = make_flag(
        "g",
        fallthrough=FallthroughConfig(
            rollout=RolloutConfig(percentage=50.0, bucket_by="missing_attr")
        ),
    )
    result = evaluator_with(flag).evaluate("g", EvalContext(user_id=None))
    assert result.reason == "error"
    assert result.variant == "control"
    assert result.rollout_bucket is None


def test_ordered_rules_stop_after_first_match():
    first = make_rule(
        [GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro")],
        rule_id="first",
    )
    second = make_rule(
        [GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro")],
        rule_id="second",
    )
    ev = evaluator_with(make_flag("g", rules=[first, second]))
    assert ev.evaluate("g", ctx(plan="pro")).rule_id == "first"


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
    ev = evaluator_with(make_flag("g", rules=[rule]))
    matched = ev.evaluate("g", ctx(x=attr)).reason == "rule_match"
    assert matched is expected


def test_exists_and_not_exists():
    exists_rule = make_rule(
        [GateCondition(attribute="x", operator=ConditionOperator.EXISTS)], rule_id="e"
    )
    ev = evaluator_with(make_flag("g", rules=[exists_rule]))
    assert ev.evaluate("g", ctx(x="yes")).reason == "rule_match"
    assert ev.evaluate("g", ctx()).reason == "fallthrough"  # x missing -> rule skipped


@pytest.mark.parametrize("present_falsy", ["", 0, False])
def test_exists_treats_falsy_values_as_present(present_falsy):
    exists_rule = make_rule(
        [GateCondition(attribute="x", operator=ConditionOperator.EXISTS)], rule_id="e"
    )
    ev = evaluator_with(make_flag("g", rules=[exists_rule]))
    # "", 0, and False are present (non-null) -> exists matches.
    assert ev.evaluate("g", ctx(x=present_falsy)).reason == "rule_match"


def test_not_exists_matches_missing_and_null():
    rule = make_rule(
        [GateCondition(attribute="x", operator=ConditionOperator.NOT_EXISTS)], rule_id="n"
    )
    ev = evaluator_with(make_flag("g", rules=[rule]))
    assert ev.evaluate("g", ctx()).reason == "rule_match"  # missing
    assert ev.evaluate("g", ctx(x=None)).reason == "rule_match"  # explicit null
    assert ev.evaluate("g", ctx(x="here")).reason == "fallthrough"  # present -> skipped


def test_caller_omitted_identity_is_absent():
    # bucket_by user_id with no user_id -> no unit -> error, not a phantom "".
    flag = make_flag("g")
    result = evaluator_with(flag).evaluate("g", EvalContext(anonymous_id="a1"))
    assert result.reason == "error"


def test_all_conditions_must_match():
    rule = make_rule([
        GateCondition(attribute="plan", operator=ConditionOperator.EQUALS, value="pro"),
        GateCondition(attribute="country", operator=ConditionOperator.EQUALS, value="US"),
    ])
    ev = evaluator_with(make_flag("g", rules=[rule]))
    assert ev.evaluate("g", ctx(plan="pro", country="US")).reason == "rule_match"
    assert ev.evaluate("g", ctx(plan="pro", country="CA")).reason == "fallthrough"


def test_user_id_is_a_resolvable_attribute():
    rule = make_rule([
        GateCondition(attribute="user_id", operator=ConditionOperator.EQUALS, value="vip")
    ])
    ev = evaluator_with(make_flag("g", rules=[rule]))
    assert ev.evaluate("g", EvalContext(user_id="vip")).reason == "rule_match"


def test_weighted_assignment_respects_weights():
    # control weight 0 -> only treatment can ever be served on assignment.
    flag = make_flag(
        "g",
        variants=[VariantConfig(key="control", weight=0), VariantConfig(key="treatment", weight=1)],
    )
    ev = evaluator_with(flag)
    assert {ev.evaluate("g", ctx(user_id=f"u{i}")).variant for i in range(50)} == {"treatment"}
