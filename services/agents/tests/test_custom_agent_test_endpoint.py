"""POST /v1/agents/custom/test — bounded, audited full-loop dry-runs."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.framework import tool_catalog, tool_loop
from app.llm.router import ToolCall, ToolCompletion
from app.main import app
from app.routers import custom_agents as router_mod
from app.store.custom_agent_tests import (
    CustomAgentTestBusyError,
    CustomAgentTestRateLimitError,
)


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
        self.fetch_result: list[dict[str, Any]] = []

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))

    async def fetchrow(self, query: str, *args: Any):
        self.executed.append((query, args))
        return None

    async def fetchval(self, query: str, *args: Any):
        self.executed.append((query, args))
        return 1

    async def fetch(self, query: str, *args: Any):
        self.executed.append((query, args))
        return self.fetch_result


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


@pytest.fixture(autouse=True)
def _stub_dry_run_admission(monkeypatch):
    calls: dict[str, list[Any]] = {"begun": [], "finished": []}

    async def fake_begin(pool, **kwargs):
        calls["begun"].append(kwargs)

    async def fake_finish(pool, test_run_id, **kwargs):
        calls["finished"].append((test_run_id, kwargs))

    monkeypatch.setattr(router_mod, "begin_custom_agent_test_run", fake_begin)
    monkeypatch.setattr(router_mod, "finish_custom_agent_test_run", fake_finish)
    return calls


@pytest.mark.asyncio
async def test_dry_run_runs_loop_and_audits_real_tool_spend(
    monkeypatch, _stub_dry_run_admission
):
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
    assert body["preset_results"] == []
    assert set(body["timings_ms"]) == {"preset_tools", "llm", "total"}

    # Dry-runs avoid product run/result state, but real query calls are audited.
    writes = [q for q, _ in pool.conn.executed if "INSERT" in q or "UPDATE" in q]
    assert len([q for q in writes if "INSERT INTO agent_audit_log" in q]) == 1
    assert not any("agent_runs" in q or "agent_run_results" in q for q in writes)
    assert _stub_dry_run_admission["begun"][0]["project_id"] == "demo"
    _, finish = _stub_dry_run_admission["finished"][0]
    assert finish["status"] == "succeeded"
    assert finish["preset_tool_calls"] == 0
    assert finish["agentic_tool_calls"] == 1
    assert finish["llm_calls"] == 2


@pytest.mark.asyncio
async def test_dry_run_executes_presets_before_the_loop(
    monkeypatch, _stub_dry_run_admission
):
    ran: list[tuple[str, dict[str, Any]]] = []

    async def fake_run_tool(ctx, name, params):
        ran.append((name, params))
        return {"flags": ["beta_checkout"]}

    async def fake_chat_with_tools(model_tier, messages, tools=None, **kwargs):
        # Preset data must already be in the user prompt on the FIRST call.
        assert "beta_checkout" in messages[1]["content"]
        return ToolCompletion(text="[]")

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", fake_chat_with_tools)

    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={
                "project_id": "demo",
                "definition": _spec(preset_tools=[{"tool": "list_flags", "params": {}}]),
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert ran == [("list_flags", {})]
    assert body["preset_results"][0]["tool"] == "list_flags"
    assert "beta_checkout" in body["preset_results"][0]["result"]
    assert "## Preset data (gathered automatically)" in body["prompt"]
    # The real preset query is individually audited.
    writes = [q for q, _ in pool.conn.executed if "INSERT" in q or "UPDATE" in q]
    assert len([q for q in writes if "INSERT INTO agent_audit_log" in q]) == 1
    _, finish = _stub_dry_run_admission["finished"][0]
    assert finish["preset_tool_calls"] == 1
    assert finish["agentic_tool_calls"] == 0
    assert finish["llm_calls"] == 1


@pytest.mark.asyncio
async def test_dry_run_rejects_invalid_preset_params(monkeypatch):
    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={
                "project_id": "demo",
                "definition": _spec(
                    preset_tools=[{"tool": "discover_events", "params": {"limit": 0}}]
                ),
            },
        )
    assert resp.status_code == 422
    assert "preset_tools[0]" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dry_run_validates_definition(monkeypatch, _stub_dry_run_admission):
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
    assert _stub_dry_run_admission["begun"] == []


@pytest.mark.asyncio
async def test_dry_run_validates_requires_but_skips_only_produces_uniqueness(
    monkeypatch, _stub_dry_run_admission
):
    async def simple_chat(*args, **kwargs):
        return ToolCompletion(text="[]")

    monkeypatch.setattr(tool_loop, "chat_completion_with_tools", simple_chat)

    unresolved_pool = _FakePool()
    async with _client(unresolved_pool) as client:
        unresolved = await client.post(
            "/v1/agents/custom/test",
            json={
                "project_id": "demo",
                "definition": _spec(requires=["missing_output"]),
            },
        )
    assert unresolved.status_code == 422
    assert "does not match" in unresolved.json()["detail"]
    assert _stub_dry_run_admission["begun"] == []

    collision_pool = _FakePool()
    collision_pool.conn.fetch_result = [
        {"agent_id": "existing", "produces": "churn_signals"}
    ]
    async with _client(collision_pool) as client:
        collision = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "definition": _spec()},
        )
    assert collision.status_code == 200
    assert len(_stub_dry_run_admission["begun"]) == 1


@pytest.mark.asyncio
async def test_zero_tool_dry_run_uses_the_same_plain_completion_path_as_real_runs(
    monkeypatch
):
    calls: list[tuple[str, list[dict[str, str]]]] = []

    async def plain_completion(*, model_tier, messages, **kwargs):
        calls.append((model_tier, messages))
        return '[{"signal": "plain"}]'

    async def forbidden_loop(*args, **kwargs):
        raise AssertionError("zero-tool drafts must not enter the tool loop")

    monkeypatch.setattr(router_mod, "chat_completion", plain_completion)
    monkeypatch.setattr(router_mod, "run_tool_loop", forbidden_loop)

    async with _client(_FakePool()) as client:
        response = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "definition": _spec(tools=[])},
        )

    assert response.status_code == 200
    assert response.json()["parsed_output"] == [{"signal": "plain"}]
    assert response.json()["tool_results"] == []
    assert calls[0][0] == "fast"
    assert [message["role"] for message in calls[0][1]] == ["system", "user"]


@pytest.mark.asyncio
async def test_failure_during_post_admission_setup_terminalizes_audit(
    monkeypatch, _stub_dry_run_admission
):
    class BrokenAgent:
        def __init__(self, definition):
            raise ValueError("cannot hydrate")

    monkeypatch.setattr(router_mod, "CustomAgent", BrokenAgent)

    async with _client(_FakePool()) as client:
        with pytest.raises(ValueError, match="cannot hydrate"):
            await client.post(
                "/v1/agents/custom/test",
                json={"project_id": "demo", "definition": _spec()},
            )

    _, finish = _stub_dry_run_admission["finished"][0]
    assert finish["status"] == "failed"
    assert finish["error"] == "ValueError: cannot hydrate"


@pytest.mark.asyncio
async def test_request_cancellation_terminalizes_audit_without_being_swallowed(
    monkeypatch, _stub_dry_run_admission
):
    async def cancel_context(self, ctx):
        raise asyncio.CancelledError("client disconnected")

    monkeypatch.setattr(router_mod.CustomAgent, "retrieve_context", cancel_context)

    async with _client(_FakePool()) as client:
        with pytest.raises(asyncio.CancelledError, match="client disconnected"):
            await client.post(
                "/v1/agents/custom/test",
                json={"project_id": "demo", "definition": _spec()},
            )

    _, finish = _stub_dry_run_admission["finished"][0]
    assert finish["status"] == "failed"
    assert finish["error"] == "CancelledError: client disconnected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (CustomAgentTestBusyError("already running"), 409),
        (CustomAgentTestRateLimitError("too many"), 429),
    ],
)
async def test_dry_run_maps_database_admission_rejections(
    monkeypatch, error, status
):
    async def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(router_mod, "begin_custom_agent_test_run", reject)
    async with _client(_FakePool()) as client:
        response = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "definition": _spec()},
        )

    assert response.status_code == status
    if status == 429:
        assert response.headers["retry-after"] == "3600"


@pytest.mark.asyncio
async def test_dry_run_maps_total_llm_failure_to_502(
    monkeypatch, _stub_dry_run_admission
):
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
    _, finish = _stub_dry_run_admission["finished"][0]
    assert finish["status"] == "failed"
    assert "RuntimeError: all providers failed" in finish["error"]


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
