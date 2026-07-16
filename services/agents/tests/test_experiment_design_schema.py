"""Strict experiment-design output is rejected, never silently repaired."""

from __future__ import annotations

import copy
import json

import pytest

from app.graphs.experiment_design import ExperimentDesignAgent


def _design() -> dict:
    return {
        "experiment_id": "exp_demo",
        "source_insight": "Checkout drop-off",
        "hypothesis": "A shorter checkout will improve purchase conversion.",
        "description": "Test a shorter checkout.",
        "treatment_spec": "Remove the optional profile step behind the experiment flag.",
        "variants": [
            {"key": "control", "weight": 50, "description": "Current checkout"},
            {"key": "treatment", "weight": 50, "description": "Short checkout"},
        ],
        "primary_metric": {
            "event": "purchase",
            "type": "conversion",
            "direction": "increase",
        },
        "targeting": {"conditions": []},
        "estimated_duration_days": 14,
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.1,
            "minimum_detectable_effect": 0.02,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 5000,
            "data_settlement_seconds": 300,
        },
        "flag_config": {
            "key": "exp_demo",
            "name": "Demo experiment",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
            "rules": [],
            "fallthrough": {
                "rollout": {"percentage": 100, "bucket_by": "user_id"}
            },
            "evaluation_mode": "client",
            "auto_disable": False,
        },
    }


def test_parse_preserves_descriptions_and_strict_flag_projection() -> None:
    parsed = ExperimentDesignAgent().parse(json.dumps([_design()]))

    assert parsed[0]["variants"][0]["description"] == "Current checkout"
    assert parsed[0]["flag_config"]["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda design: design.update({"secondary_metrics": []}),
        lambda design: design["flag_config"]["variants"][0].update(
            {"description": "must not be repaired away"}
        ),
        lambda design: design["flag_config"]["rules"].append(
            {
                "rollout": {"percentage": 10, "bucket_by": "user_id"},
            }
        ),
    ],
)
def test_parse_rejects_unknown_or_noncanonical_fields(mutate) -> None:
    design = copy.deepcopy(_design())
    mutate(design)

    with pytest.raises(ValueError, match="invalid experiment design"):
        ExperimentDesignAgent().parse(json.dumps([design]))


def test_parse_rejects_single_object_and_duplicate_ids() -> None:
    agent = ExperimentDesignAgent()
    with pytest.raises(ValueError, match="JSON array"):
        agent.parse(json.dumps(_design()))

    with pytest.raises(ValueError, match="unique experiment_id"):
        agent.parse(json.dumps([_design(), _design()]))


@pytest.mark.parametrize("field", ["experiment_id", "flag_config.key"])
def test_parse_rejects_ids_config_would_refuse(field: str) -> None:
    design = _design()
    if field == "experiment_id":
        design["experiment_id"] = "experiment with spaces"
    else:
        design["flag_config"]["key"] = "flag/with/path"

    with pytest.raises(ValueError, match="invalid experiment design"):
        ExperimentDesignAgent().parse(json.dumps([design]))
