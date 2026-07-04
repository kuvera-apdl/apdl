"""Behavior analysis agent: agentic investigation through the tool loop."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.framework import tool_catalog, tool_loop
from app.framework.tool_catalog import TOOL_CATALOG
from app.graphs.behavior_analysis import BehaviorAnalysisAgent
from app.llm.router import ToolCall, ToolCompletion


class _NullAudit:
    async def log(self, *args: Any, **kwargs: Any) -> int:
        return 1


class _NullVectorStore:
    async def search(self, **kwargs: Any) -> list:
        return []

    async def store(self, **kwargs: Any) -> int:
        return 1


def _ctx() -> Any:
    return SimpleNamespace(
        project_id="demo",
        time_range_days=7,
        run_id="run-1",
        audit=_NullAudit(),
        vector_store=_NullVectorStore(),
        autonomy_level=2,
    )


def test_declares_only_read_only_query_tools():
    agent = BehaviorAnalysisAgent()
    assert set(agent.agentic_tools) <= set(TOOL_CATALOG)
    assert "discover_events" in agent.agentic_tools
    # The investigator never needs config-mutation reach; the catalog has no
    # mutating tools at all, but the declaration should stay query-only too.
    assert agent.produces == "insights"
    assert agent.parse_as == "list"


@pytest.mark.asyncio
async def test_full_run_investigates_then_produces_insights(monkeypatch):
    tool_calls_seen: list[str] = []

    async def fake_run_tool(ctx, name, params):
        tool_calls_seen.append(name)
        if name == "discover_events":
            return {"events": [{"event_name": "page", "event_count": 100}]}
        return {"steps": [{"step": "page", "users": 100}, {"step": "signup", "users": 20}]}

    rounds = {"n": 0}

    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        rounds["n"] += 1
        assert model_tier == "reasoning"
        if rounds["n"] == 1:
            # The agent's prompt must carry project scoping.
            assert "demo" in messages[1]["content"]
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={})]
            )
        if rounds["n"] == 2:
            return ToolCompletion(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="query_funnel",
                        arguments={"steps": [{"event_name": "page"}, {"event_name": "signup"}]},
                    )
                ]
            )
        return ToolCompletion(
            text='[{"title": "Signup drop", "confidence": "high", "impact": "high"}]'
        )

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)

    agent = BehaviorAnalysisAgent()
    result = await agent.run(_ctx(), {"insights": [], "errors": []})

    assert tool_calls_seen == ["discover_events", "query_funnel"]
    assert result.output == [{"title": "Signup drop", "confidence": "high", "impact": "high"}]


@pytest.mark.asyncio
async def test_unparseable_investigation_yields_no_insights(monkeypatch):
    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        return ToolCompletion(text="I could not find anything conclusive, sorry!")

    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)

    agent = BehaviorAnalysisAgent()
    result = await agent.run(_ctx(), {"insights": [], "errors": []})
    # Prose (no JSON) must degrade to zero insights, never a pseudo-insight
    # that would satisfy downstream requires=("insights",).
    assert result.output == []
