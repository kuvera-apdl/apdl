"""Approved experiment drafts open a treatment changeset via codegen."""

from __future__ import annotations

from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs import experiment_design
from app.graphs.experiment_design import (
    ExperimentDesignAgent,
    open_treatment_changeset,
    treatment_changeset_task,
)


class _FakeAudit:
    def __init__(self) -> None:
        self.logged: list[tuple[str, str, dict]] = []

    async def log(self, run_id: str, action_type: str, config: dict, **kwargs: Any):
        self.logged.append((run_id, action_type, config))


def make_ctx(autonomy_level: int = 4) -> AgentContext:
    return AgentContext(
        pool=None,
        vector_store=None,
        audit=_FakeAudit(),
        run_id="run-1",
        project_id="apdl",
        autonomy_level=autonomy_level,
        time_range_days=7,
    )


def _design(**overrides: Any) -> dict[str, Any]:
    design = {
        "experiment_id": "exp_cta",
        "hypothesis": "A sticky CTA lifts signups.",
        "treatment_spec": "Add a sticky signup CTA to the pricing page footer.",
        "primary_metric": {"event": "signup", "type": "conversion"},
        "variants": [
            {"key": "control", "weight": 50},
            {"key": "treatment", "weight": 50, "description": "sticky CTA"},
        ],
        "flag_config": {"key": "exp_cta", "variants": []},
    }
    design.update(overrides)
    return design


# ---------------------------------------------------------------------------
# treatment_changeset_task
# ---------------------------------------------------------------------------


def test_task_carries_flag_metric_and_spec():
    title, spec = treatment_changeset_task(_design())
    assert "exp_cta" in title
    assert "`exp_cta`" in spec
    assert "sticky signup CTA" in spec
    assert "`signup`" in spec
    assert "control code path" in spec


def test_task_none_for_explicit_config_only_design():
    assert treatment_changeset_task(_design(treatment_spec="")) is None


def test_task_requires_explicit_treatment_spec():
    design = _design()
    del design["treatment_spec"]
    assert treatment_changeset_task(design) is None


def test_task_none_without_flag_key():
    design = _design(experiment_id="", flag_config={})
    assert treatment_changeset_task(design) is None


# ---------------------------------------------------------------------------
# open_treatment_changeset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_returns_changeset_id_and_links_ledger(monkeypatch):
    captured: dict[str, Any] = {}
    linked: list[tuple[str, str]] = []

    async def fake_open_changeset(**kwargs):
        captured.update(kwargs)
        return {"changeset_id": "cs-1", "status": "queued"}

    async def fake_link(pool, project_id, experiment_id, changeset_id):
        linked.append((experiment_id, changeset_id))

    monkeypatch.setattr(experiment_design, "open_changeset", fake_open_changeset)
    monkeypatch.setattr(experiment_design, "link_changeset", fake_link)

    changeset_id = await open_treatment_changeset(
        object(),
        "apdl",
        "run-1",
        _design(),
        idempotency_key="command:effect",
    )

    assert changeset_id == "cs-1"
    assert captured["project_id"] == "apdl" and captured["run_id"] == "run-1"
    assert captured["idempotency_key"] == "command:effect"
    assert captured["context"] == {
        "experiment_id": "exp_cta",
        "flag_key": "exp_cta",
    }
    assert "Do not modify or remove the control code path." in captured["constraints"]
    assert linked == [("exp_cta", "cs-1")]


@pytest.mark.asyncio
async def test_open_skips_config_only_design(monkeypatch):
    async def fail_open(**kwargs):
        raise AssertionError("must not open a changeset for a config-only design")

    monkeypatch.setattr(experiment_design, "open_changeset", fail_open)
    assert (
        await open_treatment_changeset(
            None,
            "apdl",
            "run-1",
            _design(treatment_spec=""),
            idempotency_key="command:effect",
        )
        == ""
    )


# ---------------------------------------------------------------------------
# act(): approval only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_never_opens_treatment_before_approval_even_at_l4(monkeypatch):
    agent = ExperimentDesignAgent()

    async def fake_safety(ctx, design, active, evidence):
        return {"passed": True, "risk_level": "low", "checks": []}

    async def fail_open(pool, project_id, run_id, design):
        raise AssertionError("treatment must not open before the human approves")

    monkeypatch.setattr(agent, "_safety_check", fake_safety)
    monkeypatch.setattr(experiment_design, "open_treatment_changeset", fail_open)

    output = [_design()]
    meta = await agent.act(make_ctx(autonomy_level=4), {}, {}, output)
    assert meta["needs_approval"] is True
    assert "treatment_changeset_id" not in output[0]
