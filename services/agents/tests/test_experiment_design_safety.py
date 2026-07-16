"""External experiment-safety evidence is explicit and fail closed."""

from __future__ import annotations

import json

import pytest

from app.framework import AgentContext
from app.graphs import experiment_design
from app.graphs.experiment_design import ExperimentDesignAgent
from app.safety import validator


@pytest.fixture(autouse=True)
def clear_rate_limits() -> None:
    validator._action_timestamps.clear()


def _ctx(level: int = 4) -> AgentContext:
    return AgentContext(
        pool=None,
        vector_store=None,
        audit=None,
        run_id="run-1",
        project_id="apdl",
        autonomy_level=level,
        time_range_days=7,
    )


def _design() -> dict:
    return {
        "experiment_id": "exp_checkout",
        "hypothesis": "A shorter checkout will improve purchase conversion.",
        "variants": [
            {"key": "control", "weight": 1, "description": "Current checkout"},
            {"key": "treatment", "weight": 1, "description": "Short checkout"},
        ],
        "primary_metric": {
            "event": "purchase",
            "type": "conversion",
            "direction": "increase",
        },
        "targeting": {"conditions": []},
        "flag_config": {
            "key": "checkout_flag",
            "name": "Checkout experiment",
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


async def _approve_review(*args, **kwargs) -> str:
    return json.dumps({
        "approved": True,
        "concerns": [],
        "risk_level": "low",
        "recommendations": [],
    })


@pytest.mark.asyncio
async def test_config_unavailable_is_explicit_and_requires_human_review(monkeypatch):
    async def unavailable(**kwargs):
        raise RuntimeError("config down")

    monkeypatch.setattr(experiment_design, "get_active_experiments", unavailable)
    monkeypatch.setattr(experiment_design, "chat_completion", _approve_review)
    agent = ExperimentDesignAgent()

    gathered = await agent.gather(_ctx(), {}, {})
    safety = await agent._safety_check(
        _ctx(),
        _design(),
        gathered["active_experiments"],
        gathered["active_experiments_evidence"],
    )

    assert gathered["active_experiments_evidence"]["status"] == "unavailable"
    assert safety["passed"] is True
    assert safety["evidence_complete"] is False
    assert safety["requires_approval"] is True
    checks = {check["name"]: check for check in safety["checks"]}
    assert checks["active_experiments_evidence"]["status"] == "unavailable"
    assert checks["config_conflicts"]["passed"] is None


@pytest.mark.asyncio
async def test_invalid_llm_review_is_explicit_and_requires_human_review(monkeypatch):
    async def invalid_review(*args, **kwargs):
        return '{"approved": "yes"}'

    monkeypatch.setattr(experiment_design, "chat_completion", invalid_review)
    safety = await ExperimentDesignAgent()._safety_check(
        _ctx(),
        _design(),
        [],
        {"status": "available", "source": "config"},
    )

    assert safety["passed"] is True
    assert safety["evidence_complete"] is False
    assert safety["requires_approval"] is True
    review = next(c for c in safety["checks"] if c["name"] == "llm_safety_review")
    assert review["passed"] is None
    assert review["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row, expected",
    [
        (
            {
                "key": "exp_checkout",
                "flag_key": "other_flag",
                "status": "draft",
                "primary_metric": None,
                "targeting_rules": [],
            },
            "already exists",
        ),
        (
            {
                "key": "exp_other",
                "flag_key": "other_flag",
                "status": "running",
                "primary_metric": {"event": "purchase"},
                "targeting_rules": [],
            },
            "may overlap population",
        ),
    ],
)
async def test_config_duplicate_and_population_conflicts_halt(
    monkeypatch, row, expected
):
    monkeypatch.setattr(experiment_design, "chat_completion", _approve_review)
    safety = await ExperimentDesignAgent()._safety_check(
        _ctx(),
        _design(),
        [row],
        {"status": "available", "source": "config"},
    )

    conflict = next(c for c in safety["checks"] if c["name"] == "config_conflicts")
    assert conflict["passed"] is False
    assert expected in conflict["message"]
    assert safety["passed"] is False


@pytest.mark.asyncio
async def test_missing_active_population_shape_is_marked_partial(monkeypatch):
    monkeypatch.setattr(experiment_design, "chat_completion", _approve_review)
    row = {
        "key": "exp_other",
        "flag_key": "other_flag",
        "status": "running",
        "primary_metric": {"event": "purchase"},
    }

    safety = await ExperimentDesignAgent()._safety_check(
        _ctx(),
        _design(),
        [row],
        {"status": "available", "source": "config"},
    )

    conflict = next(c for c in safety["checks"] if c["name"] == "config_conflicts")
    assert conflict["passed"] is None
    assert conflict["status"] == "partial"
    assert safety["evidence_complete"] is False
    assert safety["requires_approval"] is True
