import pytest
from pydantic import ValidationError

from app.models.schemas import FlagCreate, FlagUpdate
from app.utils import serialize_client_flag, serialize_flag, serialize_flag_collection


def make_flag() -> dict:
    return {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "state": "active",
        "owners": ["team-growth"],
        "review_by": "2099-07-01",
        "description": "Controls the checkout redesign.",
        "enabled": True,
        "default_value": False,
        "rules": [{
            "id": "rule_beta",
            "name": "Beta users",
            "conditions": [{
                "attribute": "plan",
                "operator": "equals",
                "value": "pro",
            }],
            "rollout": {"percentage": 25.0, "bucket_by": "user_id"},
        }],
        "fallthrough": {
            "value": False,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "evaluation_mode": "client",
        "auto_disable": True,
        "guardrails": [{
            "metric": "frontend_error_rate",
            "threshold": "2x_baseline",
            "scope": "page:/checkout",
            "minimum_exposures": 100,
            "window_minutes": 10,
        }],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "version": 4,
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "archived_at": None,
    }


def test_serialize_flag_returns_full_canonical_admin_shape():
    assert serialize_flag(make_flag()) == make_flag()


def test_serialize_client_flag_returns_sdk_shape_only():
    assert serialize_client_flag(make_flag()) == {
        "key": "checkout",
        "enabled": True,
        "default_value": False,
        "salt": "salt_123",
        "rules": [{
            "id": "rule_beta",
            "name": "Beta users",
            "conditions": [{
                "attribute": "plan",
                "operator": "equals",
                "value": "pro",
            }],
            "rollout": {"percentage": 25.0, "bucket_by": "user_id"},
        }],
        "fallthrough": {
            "value": False,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "version": 4,
    }


def test_serialize_flag_collection_uses_canonical_envelope():
    assert serialize_flag_collection("apdl", [make_flag()]) == {
        "schema_version": 1,
        "project_id": "apdl",
        "flags": [serialize_client_flag(make_flag())],
    }


def test_flag_create_accepts_canonical_evaluation_mode():
    flag = FlagCreate.model_validate({
        "key": "checkout",
        "name": "Checkout",
        "evaluation_mode": "server",
    })

    assert flag.evaluation_mode == "server"


def test_flag_create_rejects_state_enabled_mismatch():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate({
            "key": "checkout",
            "name": "Checkout",
            "state": "draft",
            "enabled": True,
        })


def test_flag_update_rejects_state_enabled_mismatch():
    with pytest.raises(ValidationError):
        FlagUpdate.model_validate({
            "version": 4,
            "state": "disabled",
            "enabled": True,
        })


@pytest.mark.parametrize(
    "legacy_field",
    [
        "variant_type",
        "variants",
        "rollout_percentage",
        "targeting_rules",
        "default_variant",
        "client_exposed",
    ],
)
def test_flag_create_rejects_legacy_or_unknown_fields(legacy_field):
    payload = {
        "key": "checkout",
        "name": "Checkout",
        "rules": [],
        "fallthrough": {
            "value": False,
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        legacy_field: "legacy",
    }

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(payload)


def test_flag_update_requires_version():
    with pytest.raises(ValidationError):
        FlagUpdate.model_validate({"enabled": False})


def test_frontend_error_count_guardrail_requires_at_least_one_threshold():
    payload = {
        "key": "checkout",
        "name": "Checkout",
        "guardrails": [{
            "metric": "frontend_error_count",
            "threshold": "2x_baseline",
        }],
    }

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(payload)
