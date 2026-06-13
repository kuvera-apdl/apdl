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
async def test_deploy_creates_canonical_variant_flag(monkeypatch):
    captured = {}

    async def fake_create_flag(**kwargs):
        captured["flag"] = kwargs
        return {"created": True, "flag": kwargs}

    async def fake_create_experiment_config(**kwargs):
        captured["experiment"] = kwargs
        return {"created": True, "key": kwargs["experiment_id"]}

    monkeypatch.setattr(experiment_design, "create_flag", fake_create_flag)
    monkeypatch.setattr(
        experiment_design,
        "create_experiment_config",
        fake_create_experiment_config,
    )

    experiment = {
        "experiment_id": "exp_checkout",
        "hypothesis": "Checkout changes should improve purchase conversion.",
        "description": "Test checkout changes.",
        "variants": [
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "New checkout"},
        ],
        "primary_metric": {"event": "purchase", "type": "conversion", "direction": "increase"},
        "flag_config": {
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
            "auto_disable": True,
        },
    }

    deployed = await ExperimentDesignAgent()._deploy(make_ctx(), experiment)

    assert deployed is True
    assert captured["flag"]["project_id"] == "apdl"
    assert captured["flag"]["key"] == "exp_checkout"
    assert captured["flag"]["name"] == "Checkout experiment"
    assert captured["flag"]["default_variant"] == "control"
    assert captured["flag"]["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]
    assert captured["flag"]["rules"] == []
    assert captured["flag"]["fallthrough"] == {
        "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
    }
    assert "default_value" not in captured["flag"]
    assert captured["experiment"]["flag_key"] == "exp_checkout"
    assert captured["experiment"]["variants"] == experiment["variants"]
