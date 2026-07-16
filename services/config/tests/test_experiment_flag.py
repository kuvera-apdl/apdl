"""Unit tests for the experiment→flag mapping and the typed experiment schema."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.flags import experiment_flag
from app.models.schemas import (
    ExperimentCreate,
    ExperimentMetric,
    ExperimentUpdate,
    GateRule,
    VariantConfig,
)


# ---- status → flag state ----


@pytest.mark.parametrize(
    "status,expected",
    [
        ("draft", ("draft", False)),
        ("scheduled", ("draft", False)),
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


# ---- internal lifecycle projection ----


def test_build_flag_projection_carries_lifecycle_state():
    projection = experiment_flag.build_flag_projection(
        flag_key="checkout",
        name="checkout",
        description="d",
        status="stopped",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=50.0,
        targeting_rules=[],
    )
    assert projection["state"] == "disabled"
    assert projection["enabled"] is False
    assert projection["auto_disable"] is False
    assert projection["fallthrough"]["rollout"]["percentage"] == 50.0


# ---- typed ExperimentCreate validates with the flag validators ----


def test_experiment_create_accepts_display_fields_on_variants():
    exp = ExperimentCreate(
        key="checkout",
        default_variant="control",
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
            default_variant="control",
            variants=[{"key": "control", "weight": 1}, {"key": "control", "weight": 1}],
        )


def test_experiment_create_rejects_any_non_positive_weight():
    with pytest.raises(ValidationError, match="greater than 0"):
        ExperimentCreate(
            key="checkout",
            default_variant="control",
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
        ExperimentCreate(
            key="checkout",
            status="active",
            default_variant="control",
            variants=[
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
        )


@pytest.mark.parametrize("status", ["completed", "stopped"])
def test_experiment_create_rejects_terminal_status(status):
    with pytest.raises(ValidationError):
        ExperimentCreate(
            key="checkout",
            status=status,
            default_variant="control",
            variants=[
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
        )


def test_experiment_create_requires_declared_variants_and_default():
    with pytest.raises(ValidationError) as exc_info:
        ExperimentCreate(key="checkout")

    missing = {error["loc"][0] for error in exc_info.value.errors()}
    assert missing == {"variants", "default_variant"}


@pytest.mark.parametrize("field", ["key", "flag_key"])
@pytest.mark.parametrize("value", ["exp/checkout", "has space", "-leading"])
def test_experiment_create_rejects_non_path_safe_resource_keys(field, value):
    payload = {
        "key": "checkout",
        "flag_key": "checkout-flag",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        ExperimentCreate.model_validate(payload)


@pytest.mark.parametrize("count", [1, 11])
def test_experiment_create_requires_two_to_ten_variants(count):
    variants = [
        {"key": f"variant-{index}", "weight": 1}
        for index in range(count)
    ]
    with pytest.raises(ValidationError):
        ExperimentCreate(
            key="checkout",
            default_variant="variant-0",
            variants=variants,
        )


def test_experiment_metric_allows_only_conversion():
    assert ExperimentMetric(event="purchase").type == "conversion"
    with pytest.raises(ValidationError):
        ExperimentMetric(event="revenue", type="revenue")


def test_experiment_window_is_limited_to_ninety_days():
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    common = {
        "key": "checkout",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "start_date": start,
    }

    accepted = ExperimentCreate(
        **common,
        end_date=start + timedelta(days=90),
    )
    assert accepted.end_date - accepted.start_date == timedelta(days=90)

    with pytest.raises(ValidationError, match="must not exceed 90 days"):
        ExperimentCreate(
            **common,
            end_date=start + timedelta(days=90, seconds=1),
        )


def test_experiment_update_enforces_variant_and_metric_contracts():
    with pytest.raises(ValidationError):
        ExperimentUpdate(
            version=1,
            variants=[{"key": "only", "weight": 1}],
        )
    with pytest.raises(ValidationError):
        ExperimentUpdate(
            version=1,
            primary_metric={"event": "revenue", "type": "revenue"},
        )
