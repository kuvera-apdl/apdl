import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apdl.flags.evaluator import assign_weighted_variant
from apdl.flags.models import GateConfig, VariantConfig
from apdl.flags.variant_contract import (
    MAX_TOTAL_VARIANT_WEIGHT,
    MAX_VARIANTS,
    MAX_VARIANT_WEIGHT,
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
            assert GateConfig.model_validate(payload).variants
        else:
            with pytest.raises(ValidationError):
                GateConfig.model_validate(payload)
            variants = [
                VariantConfig.model_construct(**variant)
                for variant in case["variants"]
            ]
            assert assign_weighted_variant(variants, 50.0) is None


def test_shared_high_bound_assignment_vectors():
    for case in VECTORS["assignment_cases"]:
        variants = [
            VariantConfig.model_validate(variant)
            for variant in case["variants"]
        ]
        assert (
            assign_weighted_variant(variants, case["bucket"])
            == case["expected_variant"]
        )
