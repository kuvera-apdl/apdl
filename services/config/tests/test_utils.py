import pytest
from pydantic import ValidationError

from app.models.schemas import FlagCreate, FlagDisable, FlagUpdate, GuardrailConfig
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
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
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
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "evaluation_mode": "client",
        "auto_disable": False,
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


def make_create_payload(**overrides) -> dict:
    payload = {
        "key": "checkout",
        "name": "Checkout",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "fallthrough": {
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
    }
    payload.update(overrides)
    return payload


def test_guardrail_window_is_limited_to_ninety_days():
    with pytest.raises(ValidationError, match="less than or equal to 129600"):
        GuardrailConfig(
            metric="frontend_error_count",
            threshold="at_least_one",
            window_minutes=129_601,
        )


def test_automatic_guardrail_mutation_is_rejected_by_public_writes():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(auto_disable=True)
        )
    with pytest.raises(ValidationError):
        FlagUpdate.model_validate({"version": 1, "auto_disable": True})


def test_serialize_flag_returns_full_canonical_admin_shape():
    assert serialize_flag(make_flag()) == make_flag()


def test_serialize_client_flag_returns_sdk_shape_only():
    assert serialize_client_flag(make_flag()) == {
        "key": "checkout",
        "enabled": True,
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
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
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "version": 4,
    }


def test_serializers_omit_presence_condition_value():
    flag = make_flag()
    flag["rules"][0]["conditions"] = [
        {"attribute": "plan", "operator": "exists"}
    ]

    admin_condition = serialize_flag(flag)["rules"][0]["conditions"][0]
    client_condition = serialize_client_flag(flag)["rules"][0]["conditions"][0]

    assert admin_condition == {"attribute": "plan", "operator": "exists"}
    assert client_condition == {"attribute": "plan", "operator": "exists"}


def test_serialize_client_flag_rejects_nested_old_fallthrough_value():
    flag = make_flag()
    flag["fallthrough"] = {
        "value": False,
        "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
    }

    with pytest.raises(ValidationError):
        serialize_client_flag(flag)


def test_serialize_flag_collection_uses_canonical_envelope():
    assert serialize_flag_collection("apdl", [make_flag()]) == {
        "schema_version": 2,
        "project_id": "apdl",
        "flags": [serialize_client_flag(make_flag())],
    }


def test_flag_create_accepts_canonical_evaluation_mode():
    flag = FlagCreate.model_validate(make_create_payload(evaluation_mode="server"))

    assert flag.evaluation_mode == "server"


def test_flag_create_rejects_state_enabled_mismatch():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(make_create_payload(state="draft", enabled=True))


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
        "variants_json",
        "rollout_percentage",
        "targeting_rules",
        "default_value",
        "defaultVariant",
        "client_exposed",
    ],
)
def test_flag_create_rejects_legacy_or_unknown_fields(legacy_field):
    payload = make_create_payload()
    payload[legacy_field] = "legacy"

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(payload)


@pytest.mark.parametrize(
    "legacy_field",
    [
        "variant_type",
        "variants_json",
        "rollout_percentage",
        "targeting_rules",
        "default_value",
        "defaultVariant",
        "client_exposed",
    ],
)
def test_flag_update_rejects_legacy_or_unknown_fields(legacy_field):
    payload = {"version": 4, legacy_field: "legacy"}

    with pytest.raises(ValidationError):
        FlagUpdate.model_validate(payload)


def test_flag_create_requires_default_variant():
    payload = make_create_payload()
    payload.pop("default_variant")

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(payload)


def test_flag_create_requires_variants():
    payload = make_create_payload()
    payload.pop("variants")

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(payload)


def test_flag_create_requires_fallthrough():
    payload = make_create_payload()
    payload.pop("fallthrough")

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(payload)


def test_flag_create_rejects_fallthrough_value():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(fallthrough={
                "value": False,
                "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
            })
        )


def test_flag_create_rejects_exists_condition_value():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(rules=[{
                "id": "rule_present",
                "name": "",
                "conditions": [{
                    "attribute": "plan",
                    "operator": "exists",
                    "value": None,
                }],
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            }])
        )


def test_flag_create_rejects_duplicate_variant_keys():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(variants=[
                {"key": "control", "weight": 1},
                {"key": "control", "weight": 1},
            ])
        )


def test_flag_create_rejects_default_variant_not_present_in_variants():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(
                default_variant="missing",
                variants=[
                    {"key": "control", "weight": 1},
                    {"key": "treatment", "weight": 1},
                ],
            )
        )


def test_flag_create_rejects_decimal_variant_weights():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(variants=[
                {"key": "control", "weight": 0.5},
                {"key": "treatment", "weight": 0.5},
            ])
        )


def test_flag_create_rejects_non_integer_variant_weights():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(variants=[
                {"key": "control", "weight": "1"},
                {"key": "treatment", "weight": 1},
            ])
        )


def test_flag_create_rejects_negative_variant_weights():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(variants=[
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": -1},
            ])
        )


def test_flag_create_rejects_all_zero_variant_weights():
    with pytest.raises(ValidationError):
        FlagCreate.model_validate(
            make_create_payload(variants=[
                {"key": "control", "weight": 0},
                {"key": "treatment", "weight": 0},
            ])
        )


def test_flag_update_requires_version():
    with pytest.raises(ValidationError):
        FlagUpdate.model_validate({"enabled": False})


def test_flag_disable_accepts_experiment_rollback_reason():
    body = FlagDisable.model_validate({
        "version": 4,
        "reason": "experiment_rollback",
        "evidence": {"rollback_monitor": "experiment"},
    })

    assert body.reason == "experiment_rollback"


def test_flag_disable_rejects_spoofed_source():
    with pytest.raises(ValidationError):
        FlagDisable.model_validate({"version": 4, "source": "system"})


def test_frontend_error_count_guardrail_requires_at_least_one_threshold():
    payload = {
        "guardrails": [{
            "metric": "frontend_error_count",
            "threshold": "2x_baseline",
        }],
    }

    with pytest.raises(ValidationError):
        FlagCreate.model_validate(make_create_payload(**payload))
