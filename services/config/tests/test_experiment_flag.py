"""Unit tests for the experiment→flag mapping and the typed experiment schema."""

import pytest
from pydantic import ValidationError

from app.flags import experiment_flag
from app.models.schemas import (
    ExperimentCreate,
    GateRule,
    VariantConfig,
    derive_default_variant,
)


# ---- status → flag state ----


@pytest.mark.parametrize(
    "status,expected",
    [
        ("draft", ("draft", False)),
        ("running", ("active", True)),
        ("completed", ("disabled", False)),
        ("stopped", ("disabled", False)),
    ],
)
def test_status_to_flag_state(status, expected):
    assert experiment_flag.status_to_flag_state(status) == expected


# ---- build_flag_create ----


def _variants() -> list[VariantConfig]:
    return [VariantConfig(key="control", weight=1), VariantConfig(key="treatment", weight=3)]


def test_build_flag_create_running_enables_flag():
    flag = experiment_flag.build_flag_create(
        flag_key="checkout",
        name="checkout",
        description="redesign",
        status="running",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=80.0,
        targeting_rules=[],
    )
    assert flag.key == "checkout"
    assert flag.state == "active"
    assert flag.enabled is True
    # Traffic gates via the fallthrough rollout; variants split by weight.
    assert flag.fallthrough.rollout.percentage == 80.0
    assert flag.fallthrough.rollout.bucket_by == "user_id"
    assert [v.model_dump() for v in flag.variants] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 3},
    ]


def test_build_flag_create_draft_is_disabled():
    flag = experiment_flag.build_flag_create(
        flag_key="checkout",
        name="",
        description="",
        status="draft",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=100.0,
        targeting_rules=[],
    )
    assert flag.state == "draft"
    assert flag.enabled is False
    # name defaults to the flag key when blank.
    assert flag.name == "checkout"


@pytest.mark.parametrize("status", ["completed", "stopped"])
def test_build_flag_create_terminal_disables_flag(status):
    flag = experiment_flag.build_flag_create(
        flag_key="checkout",
        name="checkout",
        description="",
        status=status,
        variants=_variants(),
        default_variant="control",
        traffic_percentage=100.0,
        targeting_rules=[],
    )
    assert flag.state == "disabled"
    assert flag.enabled is False


def test_build_flag_create_passes_targeting_rules_through():
    rule = GateRule.model_validate(
        {
            "id": "rule-1",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }
    )
    flag = experiment_flag.build_flag_create(
        flag_key="checkout",
        name="checkout",
        description="",
        status="running",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=100.0,
        targeting_rules=[rule],
    )
    assert len(flag.rules) == 1
    assert flag.rules[0].id == "rule-1"


def test_build_flag_create_respects_custom_bucket_by():
    flag = experiment_flag.build_flag_create(
        flag_key="checkout",
        name="checkout",
        description="",
        status="running",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=100.0,
        targeting_rules=[],
        bucket_by="anonymous_id",
    )
    assert flag.fallthrough.rollout.bucket_by == "anonymous_id"


# ---- build_flag_update ----


def test_build_flag_update_carries_version_and_state():
    update = experiment_flag.build_flag_update(
        version=7,
        flag_key="checkout",
        name="checkout",
        description="d",
        status="stopped",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=50.0,
        targeting_rules=[],
    )
    assert update.version == 7
    assert update.state == "disabled"
    assert update.enabled is False
    assert update.fallthrough.rollout.percentage == 50.0


# ---- default_variant derivation ----


def test_derive_default_variant_prefers_control():
    assert derive_default_variant(["treatment", "control"]) == "control"


def test_derive_default_variant_falls_back_to_first():
    assert derive_default_variant(["b", "a"]) == "b"


def test_derive_default_variant_uses_explicit_when_valid():
    assert derive_default_variant(["control", "treatment"], "treatment") == "treatment"


def test_derive_default_variant_rejects_unknown_explicit():
    with pytest.raises(ValueError, match="default_variant"):
        derive_default_variant(["control", "treatment"], "missing")


# ---- typed ExperimentCreate validates with the flag validators ----


def test_experiment_create_accepts_display_fields_on_variants():
    exp = ExperimentCreate(
        key="checkout",
        status="running",
        variants=[
            {"key": "control", "weight": 1, "description": "Current"},
            {"key": "treatment", "weight": 1, "description": "New"},
        ],
    )
    assert exp.variants[0].description == "Current"


def test_experiment_create_rejects_duplicate_variant_keys():
    with pytest.raises(ValidationError, match="unique keys"):
        ExperimentCreate(
            key="checkout",
            variants=[{"key": "control", "weight": 1}, {"key": "control", "weight": 1}],
        )


def test_experiment_create_rejects_zero_total_weight():
    with pytest.raises(ValidationError, match="positive weight"):
        ExperimentCreate(
            key="checkout",
            variants=[{"key": "control", "weight": 0}, {"key": "treatment", "weight": 0}],
        )


def test_experiment_create_rejects_default_variant_not_in_variants():
    with pytest.raises(ValidationError, match="default_variant"):
        ExperimentCreate(
            key="checkout",
            default_variant="missing",
            variants=[{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1}],
        )


def test_experiment_create_rejects_legacy_active_status():
    # The agent used to post status="active"; it is no longer a valid literal.
    with pytest.raises(ValidationError):
        ExperimentCreate(key="checkout", status="active")


def test_experiment_create_defaults_to_control_treatment():
    exp = ExperimentCreate(key="checkout")
    assert [v.key for v in exp.variants] == ["control", "treatment"]
