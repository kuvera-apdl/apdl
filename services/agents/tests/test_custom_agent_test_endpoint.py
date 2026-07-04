"""POST /v1/agents/custom/test — dry-run of the full agentic loop, zero persistence."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.framework import tool_catalog, tool_loop
from app.llm.router import ToolCall, ToolCompletion
from app.main import app


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Analyse churn for {project_id}",
        "model_tier": "fast",
        "tools": ["discover_events"],
        "requires": [],
        "produces": "churn_signals",
        "memory_query": None,
        "memory_top_k": 5,
        "pipeline_order": 60,
        "max_tool_steps": 4,
    }
    base.update(overrides)
    return base


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))

    async def fetchrow(self, query: str, *args: Any):
        self.executed.append((query, args))
        return None

    async def fetchval(self, query: str, *args: Any):
        self.executed.append((query, args))
        return 1

    async def fetch(self, query: str, *args: Any):
        return []


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _client(pool: _FakePool) -> AsyncClient:
    app.state.pg_pool = pool
    app.state.vector_store = object()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_dry_run_runs_loop_and_writes_nothing(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        assert ctx.project_id == "demo"
        assert name == "discover_events"
        return {"events": ["signup", "purchase"]}

    calls = {"n": 0}

    async def fake_chat_with_tools(model_tier, messages, tools=None, **kwargs):
        assert model_tier == "fast"
        calls["n"] += 1
        if calls["n"] == 1:
            assert tools and tools[0]["name"] == "discover_events"
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={"limit": 5})]
            )
        # The tool result must have re-entered the conversation.
        assert any(m["role"] == "tool" and "signup" in m["content"] for m in messages)
        return ToolCompletion(text='[{"signal": "activation drop"}]')

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat_with_tools)

    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "time_range_days": 14, "definition": _spec()},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["parsed_output"] == [{"signal": "activation drop"}]
    assert body["raw_response"].startswith("[")
    assert "demo" in body["prompt"]
    trace = body["tool_results"]
    assert trace[0]["tool"] == "discover_events"
    assert trace[0]["params"] == {"limit": 5}
    assert "signup" in trace[0]["result"]
    assert set(body["timings_ms"]) == {"llm", "total"}

    # The whole point of the dry run: zero DB writes — no run row, no audit
    # entries (log_tool_calls off), no memory writes.
    writes = [q for q, _ in pool.conn.executed if "INSERT" in q or "UPDATE" in q]
    assert writes == []


@pytest.mark.asyncio
async def test_dry_run_validates_definition(monkeypatch):
    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={
                "project_id": "demo",
                "definition": _spec(produces="insights"),
            },
        )
    assert resp.status_code == 422
    assert "reserved" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dry_run_maps_total_llm_failure_to_502(monkeypatch):
    async def failing_chat(*args, **kwargs):
        raise RuntimeError("all providers failed")

    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", failing_chat)

    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "definition": _spec()},
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_dry_run_truncates_oversized_tool_results(monkeypatch):
    big = {"rows": ["x" * 1000] * 50}  # ~50KB serialized

    async def fake_run_tool(ctx, name, params):
        return big

    calls = {"n": 0}

    async def fake_chat_with_tools(model_tier, messages, tools=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ToolCompletion(
                tool_calls=[ToolCall(id="c1", name="discover_events", arguments={})]
            )
        return ToolCompletion(text="[]")

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat_with_tools)

    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "definition": _spec()},
        )
    entry = resp.json()["tool_results"][0]
    assert "truncated" in entry["result"]
    assert len(entry["result"]) < 10_000
