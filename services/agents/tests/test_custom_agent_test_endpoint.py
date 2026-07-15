"""POST /v1/agents/custom/test — dry-run with zero persistence."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.framework import tool_catalog
from app.main import app
from app.routers import custom_agents as router_mod


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Data: {tool_results}",
        "model_tier": "fast",
        "tools": [{"tool": "discover_events", "params": {"limit": 5}}],
        "requires": [],
        "produces": "churn_signals",
        "parse_as": "list",
        "memory_query": None,
        "memory_top_k": 5,
        "pipeline_order": 60,
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
async def test_dry_run_returns_intermediates_and_writes_nothing(monkeypatch):
    async def fake_run_tool(ctx, name, params):
        assert ctx.project_id == "demo"
        return {"events": ["signup", "purchase"]}

    async def fake_chat(model_tier, messages, **kwargs):
        assert model_tier == "fast"
        assert messages[0]["role"] == "system"
        assert "signup" in messages[1]["content"]
        return '[{"signal": "activation drop"}]'

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(router_mod, "chat_completion", fake_chat)

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
    assert "signup" in body["prompt"]
    assert body["tool_results"][0]["result"] == {"events": ["signup", "purchase"]}
    assert set(body["timings_ms"]) == {"gather", "llm", "total"}

    # The whole point of the dry run: zero DB writes — no run row, no audit
    # entries, no memory writes.
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
    async def fake_run_tool(ctx, name, params):
        return {}

    async def failing_chat(**kwargs):
        raise RuntimeError("all providers failed")

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(router_mod, "chat_completion", failing_chat)

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

    async def fake_chat(**kwargs):
        return "{}"

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)
    monkeypatch.setattr(router_mod, "chat_completion", fake_chat)

    pool = _FakePool()
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/custom/test",
            json={"project_id": "demo", "definition": _spec()},
        )
    entry = resp.json()["tool_results"][0]
    assert entry["result"] is None
    assert len(entry["result_truncated"]) == router_mod._TOOL_RESULT_CAP
