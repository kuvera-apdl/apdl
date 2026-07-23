import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.flags.evaluator import assign_weighted_variant
from app.flags.variant_contract import (
    MAX_TOTAL_VARIANT_WEIGHT,
    MAX_VARIANTS,
    MAX_VARIANT_WEIGHT,
)
from app.models.schemas import (
    ClientFlagConfig,
    ExperimentCreate,
    FlagUpdate,
)


VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "gates"
        / "variant-weights.json"
    ).read_text()
)


def _flag(variants, default_variant):
    return {
        "key": "weight_contract",
        "enabled": True,
        "default_variant": default_variant,
        "variants": variants,
        "salt": "salt",
        "rules": [],
        "fallthrough": {
            "rollout": {
                "percentage": 100.0,
                "bucket_by": "user_id",
            }
        },
        "version": 1,
    }


def test_shared_variant_weight_validation_vectors():
    assert VECTORS["limits"] == {
        "max_variants": MAX_VARIANTS,
        "max_variant_weight": MAX_VARIANT_WEIGHT,
        "max_total_variant_weight": MAX_TOTAL_VARIANT_WEIGHT,
    }
    for case in VECTORS["validation_cases"]:
        payload = _flag(case["variants"], case["default_variant"])
        if case["valid"]:
            assert ClientFlagConfig.model_validate(payload).variants
        else:
            with pytest.raises(ValidationError):
                ClientFlagConfig.model_validate(payload)
            assert assign_weighted_variant(case["variants"], 50.0) is None


def test_shared_high_bound_assignment_vectors():
    for case in VECTORS["assignment_cases"]:
        assert (
            assign_weighted_variant(case["variants"], case["bucket"])
            == case["expected_variant"]
        )


def test_authoring_models_enforce_variant_weight_contract():
    accepted = VECTORS["validation_cases"][0]["variants"]
    ExperimentCreate.model_validate(
        {
            "key": "high_bound",
            "variants": accepted,
            "default_variant": "control",
        }
    )
    FlagUpdate.model_validate({"version": 1, "variants": accepted})

    for case in VECTORS["validation_cases"]:
        if case["valid"]:
            FlagUpdate.model_validate(
                {"version": 1, "variants": case["variants"]}
            )
        else:
            with pytest.raises(ValidationError):
                FlagUpdate.model_validate(
                    {"version": 1, "variants": case["variants"]}
                )

    maximum_count = next(
        case
        for case in VECTORS["validation_cases"]
        if case["name"] == "maximum variant count is accepted"
    )
    ExperimentCreate.model_validate(
        {
            "key": "maximum_count",
            "variants": maximum_count["variants"],
            "default_variant": maximum_count["default_variant"],
        }
    )
    for case in VECTORS["validation_cases"]:
        if case["valid"]:
            continue
        with pytest.raises(ValidationError):
            ExperimentCreate.model_validate(
                {
                    "key": f"rejected_{case['name'].replace(' ', '_')}",
                    "variants": case["variants"],
                    "default_variant": case["default_variant"],
                }
            )


def test_server_evaluator_fails_closed_for_overflowing_variants():
    invalid_flag = _flag(
        [
            {"key": "control", "weight": MAX_VARIANT_WEIGHT},
            {"key": "treatment", "weight": 1},
        ],
        "control",
    )
    invalid_flag["state"] = "active"

    from app.flags.evaluator import evaluate

    result = evaluate(
        invalid_flag,
        {"user_id": "user", "anonymous_id": "", "attributes": {}},
    )

    assert result["reason"] == "invalid_config"
    assert result["variant"] is None
