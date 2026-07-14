"""Authoring/request schema coverage for the shared targeting contract."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.flags.targeting_contract import (
    MAX_CONDITIONS_PER_RULE,
    MAX_IDENTIFIER_LENGTH,
    MAX_MEMBERSHIP_VALUES,
    MAX_RULES,
    MAX_STRING_LENGTH,
)
from app.models.schemas import EvalContext, FlagCreate, GateCondition, GateRule

FIXTURE = json.loads(
    (
        Path(__file__).parents[3] / "fixtures" / "gates" / "targeting.json"
    ).read_text(encoding="utf-8")
)


def _rule(**overrides) -> dict:
    value = {
        "id": "rule-1",
        "name": "",
        "conditions": [],
        "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
    }
    value.update(overrides)
    return value


def _flag(**overrides) -> dict:
    value = {
        "key": "checkout",
        "name": "Checkout",
        "default_variant": "control",
        "variants": [{"key": "control", "weight": 1}],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
        },
    }
    value.update(overrides)
    return value


def test_every_valid_fixture_condition_is_authorable():
    for case in FIXTURE["condition_cases"]:
        GateCondition.model_validate(case["condition"])


def test_every_invalid_fixture_condition_is_rejected_at_authoring():
    for case in FIXTURE["invalid_condition_cases"]:
        with pytest.raises(ValidationError):
            GateCondition.model_validate(case["condition"])


@pytest.mark.parametrize("operator", ["exists", "not_exists"])
def test_presence_condition_serialization_omits_value(operator):
    condition = GateCondition.model_validate(
        {"attribute": "plan", "operator": operator}
    )

    assert condition.model_dump(mode="json", exclude_none=True) == {
        "attribute": "plan",
        "operator": operator,
    }
    with pytest.raises(ValidationError):
        GateCondition.model_validate(
            {"attribute": "plan", "operator": operator, "value": None}
        )


def test_rule_identifier_name_and_condition_limits_are_enforced():
    with pytest.raises(ValidationError):
        GateRule.model_validate(_rule(id="x" * (MAX_IDENTIFIER_LENGTH + 1)))
    with pytest.raises(ValidationError):
        GateRule.model_validate(_rule(name="x" * (MAX_STRING_LENGTH + 1)))
    with pytest.raises(ValidationError):
        GateRule.model_validate(
            _rule(
                conditions=[
                    {"attribute": "plan", "operator": "exists"}
                    for _ in range(MAX_CONDITIONS_PER_RULE + 1)
                ]
            )
        )


def test_flag_rule_list_limit_is_enforced():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            _flag(rules=[_rule(id=f"rule-{index}") for index in range(MAX_RULES + 1)])
        )


def test_eval_context_ids_keys_values_and_collection_sizes_are_bounded():
    with pytest.raises(ValidationError):
        EvalContext(user_id="x" * (MAX_IDENTIFIER_LENGTH + 1))
    with pytest.raises(ValidationError):
        EvalContext(attributes={"x" * (MAX_IDENTIFIER_LENGTH + 1): "value"})
    with pytest.raises(ValidationError):
        EvalContext(attributes={"value": "x" * (MAX_STRING_LENGTH + 1)})
    with pytest.raises(ValidationError):
        EvalContext(
            attributes={f"key-{index}": index for index in range(MAX_MEMBERSHIP_VALUES + 1)}
        )
