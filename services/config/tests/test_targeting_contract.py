"""Shared strict targeting contract for the Config evaluator."""

from __future__ import annotations

import json
from pathlib import Path

from app.flags.evaluator import evaluate, matches_condition
from app.flags.targeting_contract import (
    MAX_CONDITIONS_PER_RULE,
    MAX_IDENTIFIER_LENGTH,
    MAX_MEMBERSHIP_VALUES,
    MAX_RULES,
    MAX_STRING_LENGTH,
    NUMERIC_PATTERN,
    SUPPORTED_OPERATORS,
)

FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "gates" / "targeting.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_flag(*, condition: dict | None = None, bucket_by: str = "anonymous_id") -> dict:
    rules = []
    if condition is not None:
        rules = [
            {
                "id": "fixture-rule",
                "name": "",
                "conditions": [condition],
                "rollout": {"percentage": 100.0, "bucket_by": "anonymous_id"},
            }
        ]
    return {
        "key": "targeting-fixture",
        "state": "active",
        "enabled": True,
        "default_variant": "control",
        "variants": [{"key": "control", "weight": 1}],
        "salt": "fixture-salt",
        "rules": rules,
        "fallthrough": {
            "rollout": {
                "percentage": 0.0 if condition is not None else 100.0,
                "bucket_by": bucket_by,
            }
        },
        "version": 1,
    }


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
    assert fixture_operators == SUPPORTED_OPERATORS


def test_shared_condition_cases_match_exactly() -> None:
    for case in FIXTURE["condition_cases"]:
        result = evaluate(make_flag(condition=case["condition"]), case["context"])
        expected_reason = "rule_match" if case["expected_match"] else "fallthrough_rollout"

        assert result["reason"] == expected_reason, case["name"]
        assert matches_condition(case["condition"], case["context"]) is case[
            "expected_match"
        ], case["name"]


def test_shared_invalid_conditions_fail_closed() -> None:
    context = {
        "anonymous_id": "fixture-unit",
        "attributes": {"value": "pro"},
    }
    for case in FIXTURE["invalid_condition_cases"]:
        assert matches_condition(case["condition"], context) is False, case["name"]
        result = evaluate(make_flag(condition=case["condition"]), context)
        assert result["reason"] == "fallthrough_rollout", case["name"]


def test_shared_bucket_unit_cases_match_exactly() -> None:
    for case in FIXTURE["unit_cases"]:
        result = evaluate(
            make_flag(bucket_by=case["bucket_by"]),
            case["context"],
        )
        if case["expected_available"]:
            assert result["reason"] == "fallthrough", case["name"]
            assert result["rollout_bucket"] is not None, case["name"]
            assert result["variant_bucket"] is not None, case["name"]
        else:
            assert result["reason"] == "error", case["name"]
            assert result["rollout_bucket"] is None, case["name"]
            assert result["variant_bucket"] is None, case["name"]


def test_evaluator_rejects_rule_and_condition_limit_overflow() -> None:
    flag = make_flag()
    flag["rules"] = [
        {
            "id": f"rule-{index}",
            "name": "",
            "conditions": [],
            "rollout": {"percentage": 100.0, "bucket_by": "anonymous_id"},
        }
        for index in range(MAX_RULES + 1)
    ]
    assert evaluate(flag, {"anonymous_id": "fixture-unit", "attributes": {}})[
        "reason"
    ] == "error"

    flag["rules"] = [
        {
            "id": "too-many-conditions",
            "name": "",
            "conditions": [
                {"attribute": "value", "operator": "exists"}
                for _ in range(MAX_CONDITIONS_PER_RULE + 1)
            ],
            "rollout": {"percentage": 100.0, "bucket_by": "anonymous_id"},
        }
    ]
    assert evaluate(flag, {"anonymous_id": "fixture-unit", "attributes": {}})[
        "reason"
    ] == "error"


def test_condition_value_limits_fail_closed() -> None:
    context = {
        "anonymous_id": "fixture-unit",
        "attributes": {"value": "x"},
    }
    assert not matches_condition(
        {"attribute": "a" * (MAX_IDENTIFIER_LENGTH + 1), "operator": "exists"},
        context,
    )
    assert not matches_condition(
        {
            "attribute": "value",
            "operator": "equals",
            "value": "x" * (MAX_STRING_LENGTH + 1),
        },
        context,
    )
    assert not matches_condition(
        {
            "attribute": "value",
            "operator": "in",
            "value": ["x"] * (MAX_MEMBERSHIP_VALUES + 1),
        },
        context,
    )
