"""Tool loop: bounded rounds, error isolation, truncation, audit, forced finish."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.framework import tool_catalog, tool_loop
from app.llm.router import ToolCall, ToolCompletion


class _RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    async def log(self, run_id: str, action_type: str, config: dict, **kwargs: Any) -> int:
        self.entries.append((run_id, action_type, config))
        return 1


def _ctx() -> Any:
    return SimpleNamespace(
        project_id="demo", time_range_days=7, run_id="run-1", audit=_RecordingAudit()
    )


_SCHEMAS = [{"name": "discover_events", "description": "d", "parameters": {"type": "object"}}]


@pytest.mark.asyncio
async def test_loop_returns_text_when_model_answers_immediately(monkeypatch):
    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        return ToolCompletion(text="[]")

    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)
    result = await tool_loop.run_tool_loop(
        _ctx(), agent_name="a", system_prompt="s", user_prompt="u", tool_schemas=_SCHEMAS
    )
    assert result.text == "[]"
    assert result.trace == [] and result.rounds == 0


@pytest.mark.asyncio
async def test_loop_executes_calls_feeds_results_back_and_audits(monkeypatch):
    seen_params: list[dict] = []

    async def fake_run_tool(ctx, name, params):
        seen_params.append(params)
        return {"events": ["signup"]}

    calls = {"n": 0}

    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={"limit": 3})]
            )
        # Round 2: the tool result must be in the conversation.
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert tool_msgs and "signup" in tool_msgs[0]["content"]
        assert tool_msgs[0]["tool_call_id"] == "c1"
        return ToolCompletion(text="done")

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)

    ctx = _ctx()
    result = await tool_loop.run_tool_loop(
        ctx, agent_name="probe", system_prompt="s", user_prompt="u", tool_schemas=_SCHEMAS
    )
    assert result.text == "done"
    assert seen_params == [{"limit": 3}]
    assert result.trace[0].tool == "discover_events"
    assert ctx.audit.entries[0][1] == "probe_tool_call"
    assert ctx.audit.entries[0][2]["round"] == 1


@pytest.mark.asyncio
async def test_tool_failure_becomes_result_content_not_crash(monkeypatch):
    async def failing_run_tool(ctx, name, params):
        raise RuntimeError("warehouse down")

    calls = {"n": 0}

    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={})]
            )
        # The model must see the failure as tool output so it can adapt.
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert "warehouse down" in tool_msgs[0]["content"]
        return ToolCompletion(text="degraded answer")

    monkeypatch.setattr(tool_catalog, "run_tool", failing_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)

    result = await tool_loop.run_tool_loop(
        _ctx(), agent_name="a", system_prompt="s", user_prompt="u", tool_schemas=_SCHEMAS
    )
    assert result.text == "degraded answer"
    assert result.trace[0].error and "warehouse down" in result.trace[0].error


@pytest.mark.asyncio
async def test_budget_exhaustion_forces_final_answer_without_tools(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        return {"ok": True}

    final_call: dict[str, Any] = {}

    async def greedy_chat(model_tier, messages, tools=None, **kwargs):
        if tools is None:
            # The forced-finish call: tools withheld, budget note appended.
            final_call["messages"] = messages
            return ToolCompletion(text="forced final")
        return ToolCompletion(
            tool_calls=[ToolCall(id="c", name="discover_events", arguments={})]
        )

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", greedy_chat)

    result = await tool_loop.run_tool_loop(
        _ctx(), agent_name="a", system_prompt="s", user_prompt="u",
        tool_schemas=_SCHEMAS, max_steps=2,
    )
    assert result.text == "forced final"
    assert result.rounds == 2 and len(result.trace) == 2
    assert "tool budget" in final_call["messages"][-1]["content"].lower()


@pytest.mark.asyncio
async def test_per_round_call_cap(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        return {}

    calls = {"n": 0}

    async def spammy_chat(model_tier, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolCompletion(
                tool_calls=[
                    ToolCall(id=f"c{i}", name="discover_events", arguments={})
                    for i in range(20)
                ]
            )
        return ToolCompletion(text="done")

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", spammy_chat)

    result = await tool_loop.run_tool_loop(
        _ctx(), agent_name="a", system_prompt="s", user_prompt="u", tool_schemas=_SCHEMAS
    )
    assert len(result.trace) == tool_loop.MAX_CALLS_PER_ROUND


@pytest.mark.asyncio
async def test_results_truncated_before_reentering_prompt(monkeypatch):
    async def fat_run_tool(ctx, name, params):
        return {"rows": ["x" * 1000] * 100}  # ~100KB serialized

    calls = {"n": 0}

    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={})]
            )
        tool_msg = next(m for m in messages if m["role"] == "tool")
        assert len(tool_msg["content"]) < tool_loop.RESULT_CHAR_CAP + 100
        assert "truncated" in tool_msg["content"]
        return ToolCompletion(text="done")

    monkeypatch.setattr(tool_catalog, "run_tool", fat_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)

    result = await tool_loop.run_tool_loop(
        _ctx(), agent_name="a", system_prompt="s", user_prompt="u", tool_schemas=_SCHEMAS
    )
    assert result.text == "done"


@pytest.mark.asyncio
async def test_log_tool_calls_off_writes_no_audit(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        return {}

    calls = {"n": 0}

    async def fake_chat(model_tier, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={})]
            )
        return ToolCompletion(text="done")

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat)

    ctx = _ctx()
    await tool_loop.run_tool_loop(
        ctx, agent_name="a", system_prompt="s", user_prompt="u",
        tool_schemas=_SCHEMAS, log_tool_calls=False,
    )
    assert ctx.audit.entries == []


# --- run_preset_tools (deterministic calls before reasoning) ------------------


@pytest.mark.asyncio
async def test_preset_tools_run_in_order_and_audit_as_round_zero(monkeypatch):
    ran: list[tuple[str, dict]] = []

    async def fake_run_tool(ctx, name, params):
        ran.append((name, params))
        return {"ok": name}

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)

    ctx = _ctx()
    trace = await tool_loop.run_preset_tools(
        ctx,
        agent_name="probe",
        preset_tools=[
            {"tool": "list_flags", "params": {}},
            {"tool": "discover_events", "params": {"limit": 5}},
        ],
    )

    assert ran == [("list_flags", {}), ("discover_events", {"limit": 5})]
    assert [e.tool for e in trace] == ["list_flags", "discover_events"]
    assert all(e.error is None for e in trace)
    # Audited under the same action type as loop calls, marked preset/round 0
    # so the console trace can tell the two apart.
    assert [a[1] for a in ctx.audit.entries] == ["probe_tool_call", "probe_tool_call"]
    assert all(a[2]["preset"] is True and a[2]["round"] == 0 for a in ctx.audit.entries)


@pytest.mark.asyncio
async def test_preset_tool_failure_is_contained_and_later_presets_still_run(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        if name == "query_funnel":
            raise ValueError("funnel exploded")
        return {"ok": True}

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)

    trace = await tool_loop.run_preset_tools(
        _ctx(),
        agent_name="probe",
        preset_tools=[
            {"tool": "query_funnel", "params": {}},
            {"tool": "list_flags", "params": {}},
        ],
    )

    assert trace[0].error == "ValueError: funnel exploded"
    assert trace[1].error is None and trace[1].result is not None


@pytest.mark.asyncio
async def test_preset_tools_skip_audit_when_logging_off(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        return {}

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)

    ctx = _ctx()
    await tool_loop.run_preset_tools(
        ctx,
        agent_name="probe",
        preset_tools=[{"tool": "list_flags", "params": {}}],
        log_tool_calls=False,
    )
    assert ctx.audit.entries == []
