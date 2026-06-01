"""Tests for canonical feature gate evaluation."""

import json
from pathlib import Path

from app.flags.evaluator import (
    evaluate,
    evaluate_all,
    hash_bucket,
    percentage_bucket,
)

FIXTURES_PATH = Path(__file__).parents[3] / "fixtures" / "gates" / "parity.json"


def make_flag(overrides: dict | None = None) -> dict:
    flag = {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "enabled": True,
        "description": "",
        "default_value": False,
        "rules": [],
        "fallthrough": {
            "value": False,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "client_exposed": True,
        "auto_disable": True,
        "guardrails": [],
        "version": 4,
    }
    if overrides:
        flag.update(overrides)
    return flag


def make_context(user_id: str = "user_123") -> dict:
    return {
        "user_id": user_id,
        "anonymous_id": "",
        "attributes": {"plan": "pro", "country": "US", "age": "30"},
    }


def test_disabled_flag_returns_default_value():
    result = evaluate(make_flag({"enabled": False, "default_value": True}), make_context())

    assert result["value"] is True
    assert result["reason"] == "disabled"
    assert result["config_version"] == 4


def test_first_matching_rule_serves_true_when_rollout_passes():
    flag = make_flag({
        "rules": [{
            "id": "rule_pro",
            "name": "Pro users",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }],
    })

    result = evaluate(flag, make_context())

    assert result["value"] is True
    assert result["reason"] == "rule_match"
    assert result["rule_id"] == "rule_pro"
    assert result["rollout_percentage"] == 100.0


def test_matching_rule_uses_default_when_rollout_fails():
    flag = make_flag({
        "default_value": False,
        "rules": [{
            "id": "rule_pro",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        }],
    })

    result = evaluate(flag, make_context())

    assert result["value"] is False
    assert result["reason"] == "rule_rollout"
    assert result["rule_id"] == "rule_pro"


def test_no_rule_match_uses_fallthrough():
    flag = make_flag({
        "fallthrough": {
            "value": True,
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        },
        "rules": [{
            "id": "rule_enterprise",
            "conditions": [{
                "attribute": "plan",
                "operator": "equals",
                "value": "enterprise",
            }],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }],
    })

    result = evaluate(flag, make_context())

    assert result["value"] is True
    assert result["reason"] == "fallthrough"


def test_fallthrough_rollout_returns_default_when_outside_rollout():
    flag = make_flag({
        "default_value": False,
        "fallthrough": {
            "value": True,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
    })

    result = evaluate(flag, make_context())

    assert result["value"] is False
    assert result["reason"] == "fallthrough_rollout"


def test_missing_bucket_unit_returns_error():
    flag = make_flag({
        "fallthrough": {
            "value": True,
            "rollout": {"percentage": 100.0, "bucket_by": "account_id"},
        },
    })

    result = evaluate(flag, make_context())

    assert result["value"] is False
    assert result["reason"] == "error"


def test_condition_operators():
    operators = [
        {"attribute": "plan", "operator": "not_equals", "value": "free"},
        {"attribute": "country", "operator": "in", "value": ["US", "CA"]},
        {"attribute": "age", "operator": "gte", "value": 18},
        {"attribute": "missing", "operator": "not_exists"},
    ]

    for index, condition in enumerate(operators):
        flag = make_flag({
            "rules": [{
                "id": f"rule_{index}",
                "conditions": [condition],
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            }],
        })
        assert evaluate(flag, make_context())["value"] is True


def test_evaluate_all_flags():
    results = evaluate_all(
        [
            make_flag({"key": "one", "fallthrough": {
                "value": True,
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            }}),
            make_flag({"key": "two", "enabled": False}),
        ],
        make_context(),
    )

    assert [result["value"] for result in results] == [True, False]


def test_hash_parity_fixtures():
    fixtures = _load_parity_fixtures()

    for case in fixtures["hash_cases"]:
        assert hash_bucket(case["flag_key"], case["salt"], case["unit_id"]) == case["hash"]
        assert percentage_bucket(
            case["flag_key"],
            case["salt"],
            case["unit_id"],
        ) == case["bucket"]


def test_evaluation_parity_fixtures():
    fixtures = _load_parity_fixtures()

    for case in fixtures["evaluation_cases"]:
        result = evaluate(case["flag"], case["context"])
        expected = case["result"]

        assert result == expected


def _load_parity_fixtures() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
