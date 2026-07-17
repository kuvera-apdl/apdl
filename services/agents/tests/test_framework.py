"""Tests for the agent framework: registry, gating, and the BaseAgent lifecycle.

These exercise the framework in isolation with fakes — no Postgres or LLM
provider required.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.graphs  # noqa: F401  registers the built-in agents
from app.framework import (
    AgentContext,
    BaseAgent,
    GateDecision,
    MemoryEntry,
    gate_action,
    get_agent,
    list_agents,
    register_agent,
    registered_agents,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeVectorStore:
    def __init__(self, memories: list[dict] | None = None) -> None:
        self._memories = memories or []
        self.stored: list[tuple[str, str, dict]] = []

    async def search(self, project_id: str, query: str, top_k: int = 5) -> list[dict]:
        return self._memories[:top_k]

    async def store(self, project_id: str, content: str, metadata: dict | None = None) -> int:
        self.stored.append((project_id, content, metadata or {}))
        return len(self.stored)


def make_ctx(vector_store: FakeVectorStore | None = None, **overrides: Any) -> AgentContext:
    return AgentContext(
        pool=None,  # unused by BaseAgent.run
        vector_store=vector_store or FakeVectorStore(),
        audit=None,  # unused by BaseAgent.run
        run_id=overrides.get("run_id", "run-1"),
        project_id=overrides.get("project_id", "proj1"),
        autonomy_level=overrides.get("autonomy_level", 2),
        time_range_days=overrides.get("time_range_days", 7),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_builtin_agents_registered():
    names = list_agents()
    for expected in ["behavior_analysis", "experiment_design", "personalization", "feature_proposal"]:
        assert expected in names


def test_list_agents_is_ordered_by_pipeline_order():
    reg = registered_agents()
    names = list_agents()
    orders = [reg[n].order for n in names]
    assert orders == sorted(orders)
    # behavior_analysis (produces insights) must precede its consumers.
    assert names.index("behavior_analysis") < names.index("experiment_design")


def test_consumer_agents_declare_insight_dependency():
    for name in ["experiment_design", "personalization"]:
        assert "insights" in get_agent(name).requires
    # Phase 4: feature_proposal is fed by the durable ship-verdict queue, not
    # by insights — it must be runnable in a scheduled evaluation pipeline.
    assert get_agent("feature_proposal").requires == ()


def test_duplicate_registration_raises():
    with pytest.raises(ValueError):
        @register_agent
        class Dup(BaseAgent):
            name = "behavior_analysis"  # already taken

            def build_prompt(self, ctx, state, working):
                return None


def test_register_requires_name():
    with pytest.raises(ValueError):
        @register_agent
        class NoName(BaseAgent):
            def build_prompt(self, ctx, state, working):
                return None


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "autonomy,passed,risk,expected",
    [
        (1, True, "low", GateDecision.halt),     # L1 suggest-only
        (2, False, "low", GateDecision.halt),    # failed safety
        (2, True, "low", GateDecision.approve),  # L2 routes to approval
        (3, True, "low", GateDecision.deploy),   # L3 auto-deploys low risk
        (3, True, "high", GateDecision.approve), # L3 still approves risky
        (4, True, "high", GateDecision.deploy),  # L4 full autonomy deploys risky
        (4, False, "low", GateDecision.halt),    # ...but never a failed safety check
    ],
)
def test_gate_action(monkeypatch, autonomy, passed, risk, expected):
    monkeypatch.setenv("AGENTS_ENABLE_AUTONOMOUS_MUTATIONS", "true")
    safety = {"passed": passed, "risk_level": risk}
    assert gate_action(autonomy, safety) == expected


def test_gate_action_always_require_approval():
    safety = {"passed": True, "risk_level": "low"}
    assert gate_action(4, safety, always_require_approval=True) == GateDecision.approve


@pytest.mark.parametrize("value", [None, "false", "TRUE", "1", "yes", " true "])
def test_gate_action_disables_autonomous_mutations_by_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("AGENTS_ENABLE_AUTONOMOUS_MUTATIONS", raising=False)
    else:
        monkeypatch.setenv("AGENTS_ENABLE_AUTONOMOUS_MUTATIONS", value)

    safety = {"passed": True, "risk_level": "low"}
    assert gate_action(4, safety) == GateDecision.approve


# ---------------------------------------------------------------------------
# Requirements gating
# ---------------------------------------------------------------------------

def test_requirements_met():
    agent_cls = registered_agents()["experiment_design"]
    assert not agent_cls.requirements_met({"insights": []})
    assert not agent_cls.requirements_met({})
    assert agent_cls.requirements_met({"insights": [{"title": "x"}]})


# ---------------------------------------------------------------------------
# Lifecycle (Template Method)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifecycle_runs_all_phases(monkeypatch):
    calls: dict[str, Any] = {}

    async def fake_completion(model_tier, messages, **kwargs):
        calls["tier"] = model_tier
        calls["system"] = messages[0]["content"]
        calls["user"] = messages[1]["content"]
        calls["context"] = kwargs["context"]
        return '[{"title": "found"}]'

    monkeypatch.setattr("app.framework.base.chat_completion", fake_completion)

    class SampleAgent(BaseAgent):
        name = "sample_test_agent"
        system_prompt = "SYS"
        model_tier = "fast"
        memory_query = "anything"
        produces = "samples"
        parse_as = "list"

        async def gather(self, ctx, state, working):
            return {"gathered": working["context"] + "!"}

        def build_prompt(self, ctx, state, working):
            return f"prompt with {working['gathered']}"

        async def act(self, ctx, state, working, output):
            return {"deployed": True, "count": len(output)}

        def memory_entries(self, ctx, state, working, output, action):
            return [MemoryEntry(content="remember", metadata={"type": "sample"})]

    store = FakeVectorStore(memories=[{"content": "ctx0"}])
    ctx = make_ctx(store)

    agent = SampleAgent()
    result = await agent.run(ctx, {})

    assert result.output == [{"title": "found"}]
    assert result.metadata == {"deployed": True, "count": 1}
    assert calls["tier"] == "fast"
    assert calls["system"] == "SYS"
    assert "ctx0!" in calls["user"]              # context retrieved + gathered
    assert calls["context"].project_id == "proj1"
    assert calls["context"].purpose == "agent.sample_test_agent.reason"
    assert calls["context"].data_classification == "confidential"
    assert store.stored == []
    await agent.after_result_persisted(ctx, {}, result)
    assert store.stored == [("proj1", "remember", {"type": "sample"})]


@pytest.mark.asyncio
async def test_build_prompt_none_skips_llm(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("LLM should not be called when build_prompt returns None")

    monkeypatch.setattr("app.framework.base.chat_completion", boom)

    class SkipAgent(BaseAgent):
        name = "skip_test_agent"
        produces = "things"
        parse_as = "list"

        def build_prompt(self, ctx, state, working):
            return None

    result = await SkipAgent().run(make_ctx(), {})
    assert result.output == []  # empty_output for parse_as="list"
