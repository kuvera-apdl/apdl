"""Multi-design experiment_design: insight selection with ledger dedup,
per-design gating in act(), and the approval gate's deployable filter."""

from __future__ import annotations

from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs import experiment_design
from app.graphs.experiment_design import ExperimentDesignAgent
from app.routers.approvals import _experiment_stageable
from app.store.experiments import insight_key


class _FakeAudit:
    def __init__(self) -> None:
        self.logged: list[tuple[str, str, dict]] = []

    async def log(self, run_id: str, action_type: str, config: dict, **kwargs: Any):
        self.logged.append((run_id, action_type, config))


def make_ctx(autonomy_level: int = 2, pool: Any = None) -> AgentContext:
    return AgentContext(
        pool=pool,
        vector_store=None,
        audit=_FakeAudit(),
        run_id="run-1",
        project_id="apdl",
        autonomy_level=autonomy_level,
        time_range_days=7,
    )


def _insight(title: str, action_type: str = "experiment") -> dict:
    return {"title": title, "action_type": action_type}


def _design(experiment_id: str, source: str = "") -> dict:
    return {
        "experiment_id": experiment_id,
        "source_insight": source,
        "hypothesis": f"hypothesis for {experiment_id}",
        "flag_config": {"key": experiment_id, "variants": []},
    }


# ---------------------------------------------------------------------------
# _select_insights
# ---------------------------------------------------------------------------


def test_select_insights_prefers_experiment_flavored_and_dedups_ledger():
    agent = ExperimentDesignAgent()
    insights = [
        _insight("Checkout drop-off", action_type="monitor"),
        _insight("Slow onboarding"),
        _insight("Pricing confusion"),
    ]
    ledger = [{"insight_key": insight_key("Slow onboarding"), "experiment_id": "exp_onb"}]
    selected = agent._select_insights(insights, ledger)
    # Experiment-flavored only, minus the one already designed.
    assert [i["title"] for i in selected] == ["Pricing confusion"]


def test_select_insights_falls_back_to_all_and_caps_at_max_designs():
    agent = ExperimentDesignAgent()
    insights = [_insight(f"insight {n}", action_type="monitor") for n in range(5)]
    selected = agent._select_insights(insights, [])
    assert len(selected) == agent.max_designs


def test_insight_key_normalizes_whitespace_and_case():
    assert insight_key("  Slow   Onboarding ") == insight_key({"title": "slow onboarding"})


# ---------------------------------------------------------------------------
# act(): per-design gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_routes_all_passing_designs_to_approval_at_l2(monkeypatch):
    agent = ExperimentDesignAgent()

    async def fake_safety(ctx, design, active, evidence):
        return {"passed": True, "risk_level": "low", "checks": []}

    monkeypatch.setattr(agent, "_safety_check", fake_safety)
    output = [_design("exp_a"), _design("exp_b")]
    meta = await agent.act(make_ctx(autonomy_level=2), {}, {}, output)

    assert meta["needs_approval"] is True
    assert meta["deployed_count"] == 0
    assert meta["experiment_ids"] == ["exp_a", "exp_b"]
    assert meta["experiment_id"] == "exp_a"  # singular back-compat
    assert all(d["decision"] == "approve" and d["deployed"] is False for d in output)


@pytest.mark.asyncio
async def test_act_routes_all_passing_designs_to_approval_at_l4(monkeypatch):
    agent = ExperimentDesignAgent()

    async def fake_safety(ctx, design, active, evidence):
        return {"passed": True, "risk_level": "medium", "checks": []}

    async def fail_create_draft(**kwargs):
        raise AssertionError("agent act must not call Config before human approval")

    monkeypatch.setattr(agent, "_safety_check", fake_safety)
    monkeypatch.setattr(
        experiment_design, "create_config_experiment_draft", fail_create_draft
    )
    output = [_design("exp_a"), _design("exp_b")]
    meta = await agent.act(make_ctx(autonomy_level=4), {}, {}, output)

    assert meta["deployed_count"] == 0
    assert meta["needs_approval"] is True
    assert all(d["decision"] == "approve" and d["deployed"] is False for d in output)


@pytest.mark.asyncio
async def test_act_mixed_outcomes_halted_design_never_deploys(monkeypatch):
    agent = ExperimentDesignAgent()
    async def fake_safety(ctx, design, active, evidence):
        passed = design["experiment_id"] != "exp_bad"
        return {"passed": passed, "risk_level": "low", "checks": []}

    monkeypatch.setattr(agent, "_safety_check", fake_safety)
    output = [_design("exp_good"), _design("exp_bad")]
    meta = await agent.act(make_ctx(autonomy_level=4), {}, {}, output)

    good, bad = output
    assert good["decision"] == "approve" and good["deployed"] is False
    assert bad["decision"] == "halt" and bad["deployed"] is False
    assert meta["deployed_count"] == 0


@pytest.mark.asyncio
async def test_act_records_gate_outcome_in_ledger(monkeypatch):
    agent = ExperimentDesignAgent()
    recorded: list[tuple[str, str]] = []

    async def fake_safety(ctx, design, active, evidence):
        return {"passed": design["experiment_id"] != "exp_halt", "risk_level": "low", "checks": []}

    async def fake_record(pool, project_id, run_id, design, status):
        recorded.append((design["experiment_id"], status))

    monkeypatch.setattr(agent, "_safety_check", fake_safety)
    monkeypatch.setattr(experiment_design, "record_designed_experiment", fake_record)
    output = [_design("exp_wait"), _design("exp_halt")]
    await agent.act(make_ctx(autonomy_level=2, pool=object()), {}, {}, output)

    assert recorded == [("exp_wait", "awaiting_approval"), ("exp_halt", "halted")]


# ---------------------------------------------------------------------------
# Approval gate: deployable filter
# ---------------------------------------------------------------------------


def test_experiment_stageable_legacy_item_without_decision():
    assert _experiment_stageable({"experiment_id": "exp_old"}) is True


def test_experiment_stageable_rejects_halted_and_nonapproval_items():
    assert _experiment_stageable({"decision": "halt"}) is False
    assert _experiment_stageable({"decision": "deploy"}) is False
    assert _experiment_stageable({"decision": "approve"}) is True


def test_experiment_stageable_rejects_failed_safety():
    item = {"decision": "approve", "safety_result": {"passed": False}}
    assert _experiment_stageable(item) is False
