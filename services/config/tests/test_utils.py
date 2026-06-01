import pytest
from pydantic import ValidationError

from app.models.schemas import FlagCreate, FlagUpdate
from app.utils import serialize_client_flag, serialize_flag, serialize_flag_collection


def make_flag() -> dict:
    return {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
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
        "client_exposed": True,
        "auto_disable": True,
        "guardrails": [{
            "metric": "frontend_error_rate",
            "threshold": "2x_baseline",
            "scope": "page:/checkout",
            "minimum_exposures": 100,
            "window_minutes": 10,
        }],
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


@pytest.mark.parametrize(
    "legacy_field",
    ["variant_type", "variants", "rollout_percentage", "targeting_rules", "default_variant"],
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
