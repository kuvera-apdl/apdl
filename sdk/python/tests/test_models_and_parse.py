"""Model validation and raw-payload parsing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apdl.flags.models import ConditionOperator, GateCondition
from apdl.flags.parse import parse_flag_config_result, parse_flag_configs

VALID_GATE = {
    "key": "g",
    "enabled": True,
    "default_value": False,
    "salt": "s",
    "rules": [],
    "fallthrough": {"value": True, "rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
    "version": 3,
}


def test_value_operator_requires_value():
    with pytest.raises(ValidationError):
        GateCondition(attribute="x", operator=ConditionOperator.EQUALS)


def test_value_operator_rejects_null_value():
    with pytest.raises(ValidationError):
        GateCondition.model_validate(
            {"attribute": "x", "operator": "equals", "value": None}
        )


def test_presence_operator_rejects_value():
    with pytest.raises(ValidationError):
        GateCondition.model_validate(
            {"attribute": "x", "operator": "exists", "value": 1}
        )


def test_presence_operator_ok_without_value():
    cond = GateCondition.model_validate({"attribute": "x", "operator": "not_exists"})
    assert cond.operator is ConditionOperator.NOT_EXISTS


def test_unknown_key_rejected():
    with pytest.raises(ValidationError):
        GateCondition.model_validate(
            {"attribute": "x", "operator": "exists", "extra": 1}
        )


def test_parse_bare_list():
    result = parse_flag_config_result([VALID_GATE])
    assert len(result.flags) == 1
    assert result.invalid_keys == []


def test_parse_envelope():
    payload = {"schema_version": 1, "project_id": "p1", "flags": [VALID_GATE]}
    result = parse_flag_config_result(payload)
    assert result.flags[0].key == "g"


def test_parse_collects_invalid_keys():
    bad = {"key": "broken", "enabled": "not-a-bool"}
    result = parse_flag_config_result([VALID_GATE, bad])
    assert [f.key for f in result.flags] == ["g"]
    assert result.invalid_keys == ["broken"]


def test_parse_unrecognizable_payload_returns_none():
    assert parse_flag_config_result("nonsense") is None
    assert parse_flag_config_result({"unexpected": 1}) is None


def test_parse_wrong_schema_version_returns_none():
    assert parse_flag_config_result({"schema_version": 2, "flags": []}) is None


def test_strict_parse_rejects_any_invalid():
    assert parse_flag_configs([VALID_GATE, {"key": "x"}]) is None
    assert parse_flag_configs([VALID_GATE]) is not None
