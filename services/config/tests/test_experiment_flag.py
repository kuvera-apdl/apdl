"""Unit tests for the experiment→flag mapping and the typed experiment schema."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.flags import experiment_flag
from app.flags.evaluator import evaluate
from app.models.schemas import (
    ExperimentCreate,
    ExperimentMetric,
    ExperimentTargetingRule,
    ExperimentUpdate,
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
        bucket_by="user_id",
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
        bucket_by="user_id",
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
        bucket_by="user_id",
    )
    assert flag.state == "disabled"
    assert flag.enabled is False


def test_build_flag_create_projects_eligibility_rules_and_excludes_nonmatches():
    rule = ExperimentTargetingRule.model_validate(
        {
            "id": "rule-1",
            "name": "Paid plan",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        }
    )
    flag = experiment_flag.build_flag_create(
        flag_key="checkout",
        name="checkout",
        description="",
        status="running",
        variants=_variants(),
        default_variant="control",
        traffic_percentage=37.5,
        targeting_rules=[rule],
        bucket_by="user_id",
    )
    assert len(flag.rules) == 1
    assert flag.rules[0].id == "rule-1"
    assert flag.rules[0].rollout.percentage == 37.5
    assert flag.rules[0].rollout.bucket_by == "user_id"
    assert flag.fallthrough.rollout.percentage == 0.0
    assert flag.fallthrough.rollout.bucket_by == "user_id"


def test_experiment_targeting_rule_rejects_competing_rollout_authority():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExperimentTargetingRule.model_validate(
            {
                "id": "rule-1",
                "name": "",
                "conditions": [],
                "rollout": {"percentage": 50.0, "bucket_by": "user_id"},
            }
        )


def test_experiment_targeting_rule_requires_canonical_name_field():
    with pytest.raises(ValidationError, match="name"):
        ExperimentTargetingRule.model_validate(
            {
                "id": "rule-1",
                "conditions": [],
            }
        )


def test_anonymous_experiment_projection_enrolls_without_user_id():
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
    result = evaluate(
        flag.model_dump(mode="json"),
        {
            "anonymous_id": "anonymous-browser",
            "attributes": {},
        },
    )
    assert result["reason"] == "fallthrough"
    assert result["bucket_by"] == "anonymous_id"
    assert result["variant"] in {"control", "treatment"}


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
        bucket_by="user_id",
    )
    assert projection["state"] == "disabled"
    assert projection["enabled"] is False
    assert projection["auto_disable"] is False
    assert projection["fallthrough"]["rollout"]["percentage"] == 50.0


# ---- typed ExperimentCreate validates with the flag validators ----


def test_experiment_create_accepts_display_fields_on_variants():
    exp = ExperimentCreate(
        key="checkout",
        bucket_by="anonymous_id",
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
            bucket_by="anonymous_id",
            default_variant="control",
            variants=[{"key": "control", "weight": 1}, {"key": "control", "weight": 1}],
        )


def test_experiment_create_rejects_any_non_positive_weight():
    with pytest.raises(ValidationError, match="greater than 0"):
        ExperimentCreate(
            key="checkout",
            bucket_by="anonymous_id",
            default_variant="control",
            variants=[{"key": "control", "weight": 0}, {"key": "treatment", "weight": 0}],
        )


def test_experiment_create_rejects_default_variant_not_in_variants():
    with pytest.raises(ValidationError, match="default_variant"):
        ExperimentCreate(
            key="checkout",
            bucket_by="anonymous_id",
            default_variant="missing",
            variants=[{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1}],
        )


def test_experiment_create_rejects_legacy_active_status():
    # The agent used to post status="active"; it is no longer a valid literal.
    with pytest.raises(ValidationError):
        ExperimentCreate(
            key="checkout",
            bucket_by="anonymous_id",
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
            bucket_by="anonymous_id",
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
    assert missing == {"bucket_by", "variants", "default_variant"}


@pytest.mark.parametrize("field", ["key", "flag_key"])
@pytest.mark.parametrize("value", ["exp/checkout", "has space", "-leading"])
def test_experiment_create_rejects_non_path_safe_resource_keys(field, value):
    payload = {
        "key": "checkout",
        "flag_key": "checkout-flag",
        "bucket_by": "anonymous_id",
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
            bucket_by="anonymous_id",
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
        "bucket_by": "anonymous_id",
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


@pytest.mark.parametrize("bucket_by", [None, "account_id", ""])
def test_experiment_create_requires_explicit_actor_identity(bucket_by):
    payload = {
        "key": "checkout",
        "bucket_by": bucket_by,
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
    }
    with pytest.raises(ValidationError):
        ExperimentCreate.model_validate(payload)


def test_experiment_update_bucket_identity_is_optional_but_never_null():
    assert ExperimentUpdate(version=1).bucket_by is None
    assert (
        ExperimentUpdate(version=1, bucket_by="anonymous_id").bucket_by
        == "anonymous_id"
    )
    with pytest.raises(ValidationError, match="bucket_by must not be null"):
        ExperimentUpdate.model_validate({"version": 1, "bucket_by": None})
    with pytest.raises(ValidationError):
        ExperimentUpdate.model_validate(
            {"version": 1, "bucket_by": "account_id"}
        )
