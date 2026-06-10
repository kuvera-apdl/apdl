"""Canonical variant model validation and raw-payload parsing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apdl.flags.models import (
    ConditionOperator,
    GateCondition,
    GateConfig,
    GateEvaluationResult,
)
from apdl.flags.parse import parse_flag_config_result, parse_flag_configs

VALID_FLAG = {
    "key": "g",
    "enabled": True,
    "default_variant": "control",
    "variants": [{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1}],
    "salt": "s",
    "rules": [],
    "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
    "version": 3,
}


def _flag(**overrides):
    payload = {
        k: (v.copy() if isinstance(v, (list, dict)) else v)
        for k, v in VALID_FLAG.items()
    }
    payload.update(overrides)
    return payload


def envelope(flags=None):
    return {"schema_version": 2, "project_id": "p1", "flags": flags or [VALID_FLAG]}


# ── Conditions ────────────────────────────────────────────────


def test_value_operator_requires_value():
    with pytest.raises(ValidationError):
        GateCondition(attribute="x", operator=ConditionOperator.EQUALS)


def test_value_operator_rejects_null_value():
    with pytest.raises(ValidationError):
        GateCondition.model_validate({"attribute": "x", "operator": "equals", "value": None})


def test_presence_operator_rejects_value():
    with pytest.raises(ValidationError):
        GateCondition.model_validate({"attribute": "x", "operator": "exists", "value": 1})


def test_presence_operator_ok_without_value():
    cond = GateCondition.model_validate({"attribute": "x", "operator": "not_exists"})
    assert cond.operator is ConditionOperator.NOT_EXISTS


def test_unknown_condition_key_rejected():
    with pytest.raises(ValidationError):
        GateCondition.model_validate({"attribute": "x", "operator": "exists", "extra": 1})


# ── Canonical flag accepted ───────────────────────────────────


def test_canonical_flag_accepted():
    flag = GateConfig.model_validate(VALID_FLAG)
    assert flag.default_variant == "control"
    assert [v.key for v in flag.variants] == ["control", "treatment"]


def test_zero_weight_alongside_positive_allowed():
    flag = GateConfig.model_validate(
        _flag(variants=[{"key": "control", "weight": 0}, {"key": "treatment", "weight": 1}])
    )
    assert flag.variants[0].weight == 0


# ── Rejected legacy / non-canonical flag fields ───────────────


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_value": False},
        {"fallthrough": {"value": True, "rollout": {"percentage": 100.0, "bucket_by": "user_id"}}},
        {"variant_type": "boolean"},
        {"variants_json": "[]"},
        {"targeting_rules": []},
        {"rollout_percentage": 10},
    ],
    ids=["default_value", "fallthrough.value", "variant_type", "variants_json", "targeting_rules", "rollout_percentage"],
)
def test_legacy_flag_fields_rejected(overrides):
    with pytest.raises(ValidationError):
        GateConfig.model_validate(_flag(**overrides))


def test_camel_default_variant_rejected():
    payload = {k: v for k, v in VALID_FLAG.items() if k != "default_variant"}
    payload["defaultVariant"] = "control"
    with pytest.raises(ValidationError):
        GateConfig.model_validate(payload)


# ── Variant invariants ────────────────────────────────────────


@pytest.mark.parametrize(
    "variants",
    [
        [{"key": "control", "weight": 1}, {"key": "control", "weight": 1}],  # duplicate keys
        [{"key": "control", "weight": 0}, {"key": "treatment", "weight": 0}],  # all-zero
        [{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1.5}],  # decimal
        [{"key": "control", "weight": 1.0}, {"key": "treatment", "weight": 1}],  # float-int
        [{"key": "control", "weight": -1}, {"key": "treatment", "weight": 2}],  # negative
        [{"key": "control", "weight": True}, {"key": "treatment", "weight": 1}],  # bool
        [{"key": "control", "weight": "1"}, {"key": "treatment", "weight": 1}],  # string
        [{"key": "", "weight": 1}, {"key": "treatment", "weight": 1}],  # empty key
        [],  # empty variants
    ],
    ids=["dup", "all-zero", "decimal", "float-int", "negative", "bool", "string", "empty-key", "empty"],
)
def test_invalid_variants_rejected(variants):
    with pytest.raises(ValidationError):
        GateConfig.model_validate(_flag(variants=variants))


def test_default_variant_must_be_in_variants():
    with pytest.raises(ValidationError):
        GateConfig.model_validate(_flag(default_variant="ghost"))


def test_default_variant_required_and_non_empty():
    with pytest.raises(ValidationError):
        GateConfig.model_validate({k: v for k, v in VALID_FLAG.items() if k != "default_variant"})
    with pytest.raises(ValidationError):
        GateConfig.model_validate(_flag(default_variant=""))


@pytest.mark.parametrize("version", [0, True, 1.0], ids=["zero", "bool", "float"])
def test_invalid_version_rejected(version):
    with pytest.raises(ValidationError):
        GateConfig.model_validate(_flag(version=version))


# ── Evaluation result shape ───────────────────────────────────


def test_result_rejects_legacy_value_and_bucket():
    with pytest.raises(ValidationError):
        GateEvaluationResult(key="g", reason="disabled", value=True)
    with pytest.raises(ValidationError):
        GateEvaluationResult(key="g", reason="fallthrough", bucket=1.0)


def test_result_defaults_are_null_not_sentinels():
    r = GateEvaluationResult(key="g", reason="not_found")
    assert r.variant is None
    assert r.rule_id is None
    assert r.rollout_bucket is None
    assert r.variant_bucket is None
    assert r.config_version is None
    assert r.source is None


def test_result_source_none_string_rejected():
    with pytest.raises(ValidationError):
        GateEvaluationResult(key="g", reason="disabled", source="none")


# ── Parser: strict v2 envelope ────────────────────────────────


def test_parse_v2_envelope():
    result = parse_flag_config_result(envelope())
    assert result is not None
    assert result.flags[0].key == "g"
    assert result.invalid_keys == []


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "project_id": "p1", "flags": [VALID_FLAG]},
        [VALID_FLAG],  # bare list
        {"project_id": "p1", "flags": [VALID_FLAG]},  # missing schema_version
        {"schema_version": 2, "flags": [VALID_FLAG]},  # missing project_id
        {"schema_version": 2, "project_id": "", "flags": [VALID_FLAG]},  # empty project_id
        {"schema_version": 2, "project_id": "p1", "flags": "x"},  # non-list flags
        {"schema_version": 2, "project_id": "p1", "flags": [], "extra": 1},  # unknown key
        "nonsense",
    ],
    ids=["v1", "bare-list", "no-schema", "no-project", "empty-project", "bad-flags", "unknown-key", "non-dict"],
)
def test_parse_rejects_non_canonical_payloads(payload):
    assert parse_flag_config_result(payload) is None


def test_parse_collects_invalid_keys():
    bad = {"key": "broken", "enabled": "not-a-bool"}
    result = parse_flag_config_result(envelope([VALID_FLAG, bad]))
    assert result is not None
    assert [f.key for f in result.flags] == ["g"]
    assert result.invalid_keys == ["broken"]


def test_parse_legacy_boolean_flag_is_invalid():
    legacy = {
        "key": "old",
        "enabled": True,
        "default_value": False,
        "salt": "s",
        "rules": [],
        "fallthrough": {"value": True, "rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
        "version": 1,
    }
    result = parse_flag_config_result(envelope([legacy]))
    assert result is not None
    assert result.flags == []
    assert result.invalid_keys == ["old"]


def test_strict_parse_rejects_any_invalid():
    assert parse_flag_configs(envelope([VALID_FLAG, {"key": "x"}])) is None
    assert parse_flag_configs(envelope()) is not None
