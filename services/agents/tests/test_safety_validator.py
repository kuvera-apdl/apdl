import pytest

from app.safety.validator import ActionType, AgentAction, SafetyValidator


def make_experiment(flag_config: dict | None = None) -> dict:
    return {
        "experiment_id": "exp_checkout",
        "hypothesis": "Checkout changes should improve purchase conversion.",
        "variants": [
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "New checkout"},
        ],
        "primary_metric": {"event": "purchase", "type": "conversion", "direction": "increase"},
        "flag_config": flag_config
        or {
            "key": "exp_checkout",
            "name": "Checkout experiment",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
            "rules": [],
            "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            "evaluation_mode": "client",
            "auto_disable": False,
        },
    }


def validate_experiment(experiment: dict) -> dict:
    result = SafetyValidator().validate(
        AgentAction(
            type=ActionType.create_experiment,
            config=experiment,
            project_id="apdl",
        )
    )
    return result.model_dump()


def test_create_experiment_accepts_canonical_variant_flag_config():
    result = validate_experiment(make_experiment())

    assert result["passed"] is True
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["variant_config"]["passed"] is True
    assert checks["blast_radius"]["passed"] is True
    assert checks["guardrails"] == {
        "name": "guardrails",
        "passed": True,
        "message": "Required experiment design fields are present.",
    }


def test_create_experiment_treats_variant_weights_as_relative():
    experiment = make_experiment({
        "key": "exp_checkout",
        "name": "Checkout experiment",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 100},
            {"key": "treatment", "weight": 100},
        ],
        "rules": [],
        "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
    })

    result = validate_experiment(experiment)

    assert result["passed"] is True
    assert {check["name"]: check for check in result["checks"]}["blast_radius"]["passed"] is True


@pytest.mark.parametrize(
    ("flag_config", "message"),
    [
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "variants": [{"key": "control", "weight": 1}],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "default_variant",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 1},
                    {"key": "control", "weight": 1},
                ],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "unique keys",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "missing",
                "variants": [
                    {"key": "control", "weight": 1},
                    {"key": "treatment", "weight": 1},
                ],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "default_variant must match",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 0.5},
                    {"key": "treatment", "weight": 1},
                ],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "positive integers",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": -1},
                    {"key": "treatment", "weight": 1},
                ],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "positive integers",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 0},
                    {"key": "treatment", "weight": 0},
                ],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "positive integers",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "control",
                "default_value": False,
                "variants": [{"key": "control", "weight": 1}],
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            },
            "non-canonical",
        ),
        (
            {
                "key": "exp_checkout",
                "name": "Checkout experiment",
                "default_variant": "control",
                "variants": [{"key": "control", "weight": 1}],
                "rules": [],
                "fallthrough": {
                    "value": False,
                    "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
                },
            },
            "fallthrough must only contain rollout",
        ),
    ],
)
def test_create_experiment_rejects_non_canonical_variant_flag_config(flag_config, message):
    result = validate_experiment(make_experiment(flag_config))

    assert result["passed"] is False
    variant_check = {check["name"]: check for check in result["checks"]}["variant_config"]
    assert variant_check["passed"] is False
    assert message in variant_check["message"]


def test_create_experiment_rejects_excessive_non_default_exposure():
    experiment = make_experiment({
        "key": "exp_checkout",
        "name": "Checkout experiment",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 3},
        ],
        "rules": [],
        "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
    })

    result = validate_experiment(experiment)
    blast_radius = {check["name"]: check for check in result["checks"]}["blast_radius"]

    assert result["passed"] is False
    assert blast_radius["passed"] is False
    assert "exceeding the 50% safety limit" in blast_radius["message"]


def test_create_experiment_blast_radius_accounts_for_rule_rollouts():
    experiment = make_experiment({
        "key": "exp_checkout",
        "name": "Checkout experiment",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 3},
        ],
        "rules": [
            {
                "id": "all_users",
                "name": "All users",
                "conditions": [],
                "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
            },
        ],
        "fallthrough": {"rollout": {"percentage": 0.0, "bucket_by": "user_id"}},
    })

    result = validate_experiment(experiment)
    blast_radius = {check["name"]: check for check in result["checks"]}["blast_radius"]

    assert result["passed"] is False
    assert blast_radius["passed"] is False
    assert "75.0%" in blast_radius["message"]


def test_update_flag_rejects_legacy_boolean_field():
    result = SafetyValidator().validate(
        AgentAction(
            type=ActionType.update_flag,
            config={"key": "checkout", "default_value": False},
            project_id="apdl",
        )
    ).model_dump()

    variant_check = {check["name"]: check for check in result["checks"]}["variant_config"]
    assert result["passed"] is False
    assert variant_check["passed"] is False
    assert "non-canonical" in variant_check["message"]
