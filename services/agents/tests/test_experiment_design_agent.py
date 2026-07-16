import pytest

from app.framework import AgentContext
from app.graphs import experiment_design
from app.graphs.experiment_design import ExperimentDesignAgent


def make_ctx() -> AgentContext:
    return AgentContext(
        pool=None,
        vector_store=None,
        audit=None,
        run_id="run-1",
        project_id="apdl",
        autonomy_level=3,
        time_range_days=7,
    )


@pytest.mark.asyncio
async def test_deploy_uses_single_config_owned_creation_path(monkeypatch):
    captured = {}

    async def fake_create_experiment_config(**kwargs):
        captured["experiment"] = kwargs
        return {"created": True, "key": kwargs["experiment_id"], "flag_key": kwargs.get("flag_key")}

    monkeypatch.setattr(
        experiment_design,
        "create_experiment_config",
        fake_create_experiment_config,
    )

    # Config now owns flag init — the agent must not import or call create_flag.
    assert not hasattr(experiment_design, "create_flag")

    experiment = {
        "experiment_id": "exp_checkout",
        "hypothesis": "Checkout changes should improve purchase conversion.",
        "description": "Test checkout changes.",
        "variants": [
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "New checkout"},
        ],
        "primary_metric": {"event": "purchase", "type": "conversion", "direction": "increase"},
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.5,
            "minimum_detectable_effect": 0.5,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 20,
            "data_settlement_seconds": 300,
        },
        "flag_config": {
            "key": "exp_checkout",
            "name": "Checkout experiment",
            "default_variant": "control",
            "variants": [
                {"key": "control", "weight": 1},
                {"key": "treatment", "weight": 1},
            ],
        },
    }

    deployed = await ExperimentDesignAgent()._deploy(make_ctx(), experiment)

    assert deployed is True
    # Exactly one creation path, carrying the canonical link + variants.
    assert captured["experiment"]["experiment_id"] == "exp_checkout"
    assert captured["experiment"]["flag_key"] == "exp_checkout"
    assert captured["experiment"]["variants"] == experiment["variants"]
    assert captured["experiment"]["primary_metric"] == experiment["primary_metric"]
    assert captured["experiment"]["statistical_plan"] == experiment["statistical_plan"]
