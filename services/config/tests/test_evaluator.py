"""Tests for canonical feature flag evaluation."""

import json
from pathlib import Path

import pytest

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
        "state": "active",
        "enabled": True,
        "description": "",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "evaluation_mode": "client",
        "auto_disable": False,
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


@pytest.mark.parametrize(
    "percentage",
    ["50", True, None, float("nan"), float("inf"), -1.0, 101.0],
)
def test_corrupt_fallthrough_rollout_fails_closed(percentage):
    flag = make_flag(
        {
            "fallthrough": {
                "rollout": {
                    "percentage": percentage,
                    "bucket_by": "user_id",
                }
            }
        }
    )

    result = evaluate(flag, make_context())

    assert result["key"] == "checkout"
    assert result["variant"] is None
    assert result["reason"] == "invalid_config"
    assert result["config_version"] == 4


def test_corrupt_rule_rollout_fails_closed_before_rule_matching():
    flag = make_flag(
        {
            "rules": [
                {
                    "id": "corrupt",
                    "conditions": [],
                    "rollout": {
                        "percentage": "100",
                        "bucket_by": "user_id",
                    },
                }
            ]
        }
    )

    result = evaluate(flag, make_context())

    assert result["variant"] is None
    assert result["reason"] == "invalid_config"


def test_disabled_flag_returns_default_variant():
    result = evaluate(make_flag({"enabled": False, "default_variant": "treatment"}), make_context())

    assert result["variant"] == "treatment"
    assert result["reason"] == "disabled"
    assert result["config_version"] == 4


def test_draft_flag_returns_default_variant():
    result = evaluate(make_flag({"state": "draft", "default_variant": "treatment"}), make_context())

    assert result["variant"] == "treatment"
    assert result["reason"] == "disabled"


def test_first_matching_rule_assigns_variant_when_rollout_passes():
    flag = make_flag({
        "rules": [{
            "id": "rule_pro",
            "name": "Pro users",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }],
    })

    result = evaluate(flag, make_context())

    assert result["variant"] in {"control", "treatment"}
    assert result["reason"] == "rule_match"
    assert result["rule_id"] == "rule_pro"
    assert result["rollout_bucket"] is not None
    assert result["variant_bucket"] is not None
    assert result["rollout_percentage"] == 100.0


def test_matching_rule_uses_default_when_rollout_fails():
    flag = make_flag({
        "rules": [{
            "id": "rule_pro",
            "name": "Pro users",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        }],
    })

    result = evaluate(flag, make_context())

    assert result["variant"] == "control"
    assert result["reason"] == "rule_rollout"
    assert result["rule_id"] == "rule_pro"


def test_no_rule_match_uses_fallthrough():
    flag = make_flag({
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        },
        "rules": [{
            "id": "rule_enterprise",
            "name": "Enterprise users",
            "conditions": [{
                "attribute": "plan",
                "operator": "equals",
                "value": "enterprise",
            }],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }],
    })

    result = evaluate(flag, make_context())

    assert result["variant"] in {"control", "treatment"}
    assert result["reason"] == "fallthrough"
    assert result["variant_bucket"] is not None


def test_fallthrough_rollout_returns_default_when_outside_rollout():
    flag = make_flag({
        "fallthrough": {
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
    })

    result = evaluate(flag, make_context())

    assert result["variant"] == "control"
    assert result["reason"] == "fallthrough_rollout"


def test_missing_bucket_unit_returns_error():
    flag = make_flag({
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "account_id"},
        },
    })

    result = evaluate(flag, make_context())

    assert result["variant"] == "control"
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
                "name": "",
                "conditions": [condition],
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            }],
        })
        assert evaluate(flag, make_context())["reason"] == "rule_match"


def test_exists_uses_presence_and_non_null_value():
    flag = make_flag({
        "rules": [{
            "id": "rule_presence",
            "name": "",
            "conditions": [
                {"attribute": "empty_text", "operator": "exists"},
                {"attribute": "is_beta", "operator": "exists"},
                {"attribute": "cart_items", "operator": "exists"},
                {"attribute": "null_trait", "operator": "not_exists"},
                {"attribute": "missing_trait", "operator": "not_exists"},
            ],
            "rollout": {"percentage": 100.0, "bucket_by": "anonymous_id"},
        }],
    })

    result = evaluate(flag, {
        "anonymous_id": "anon_123",
        "attributes": {
            "empty_text": "",
            "is_beta": False,
            "cart_items": 0,
            "null_trait": None,
        },
    })

    assert result["reason"] == "rule_match"


def test_evaluate_all_flags():
    results = evaluate_all(
        [
            make_flag({"key": "one", "fallthrough": {
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            }}),
            make_flag({"key": "two", "enabled": False}),
        ],
        make_context(),
    )

    assert [result["variant"] for result in results] == ["treatment", "control"]


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
        expected = {**case["result"], "source": None}

        assert result == expected


def _load_parity_fixtures() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
