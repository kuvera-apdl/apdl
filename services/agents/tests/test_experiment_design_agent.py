import pytest

from app.graphs import experiment_design


@pytest.mark.asyncio
async def test_staging_uses_single_config_owned_draft_path(monkeypatch):
    captured = {}

    async def fake_create_experiment_draft(**kwargs):
        captured["experiment"] = kwargs
        return {"created": True, "key": kwargs["experiment_id"], "flag_key": kwargs.get("flag_key")}

    monkeypatch.setattr(
        experiment_design,
        "create_config_experiment_draft",
        fake_create_experiment_draft,
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

    drafted = await experiment_design.stage_experiment_draft("apdl", experiment)

    assert drafted is True
    # Exactly one creation path, carrying the canonical link + variants.
    assert captured["experiment"]["experiment_id"] == "exp_checkout"
    assert captured["experiment"]["flag_key"] == "exp_checkout"
    assert captured["experiment"]["variants"] == experiment["variants"]
    assert captured["experiment"]["primary_metric"] == experiment["primary_metric"]
    assert captured["experiment"]["statistical_plan"] == experiment["statistical_plan"]
    assert "estimated_duration_days" not in captured["experiment"]
    assert "secondary_metrics" not in captured["experiment"]
    assert "guardrail_metrics" not in captured["experiment"]
