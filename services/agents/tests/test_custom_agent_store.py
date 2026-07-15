"""custom_agents store: DDL shape, JSONB serialization, defensive parsing."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.store import custom_agents as store


def _definition(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "Watches churn signals",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Analyse churn",
        "model_tier": "fast",
        "tools": ["discover_events", "query_events"],
        "requires": [],
        "produces": "churn_signals",
        "memory_query": None,
        "memory_top_k": 5,
        "pipeline_order": 60,
        "max_tool_steps": 8,
    }
    base.update(overrides)
    return base


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "agent_id": "agent-1",
        "project_id": "demo",
        "status": "active",
        "created_at": "2026-07-02T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z",
        **_definition(),
    }
    row.update(overrides)
    return row


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetchrow_result: dict[str, Any] | None = None
        self.fetch_result: list[dict[str, Any]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append((query, args))
        return self.fetchrow_result

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
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


def test_ddl_enforces_active_slug_uniqueness_only():
    # Partial unique index: archive-then-recreate under the same slug works.
    assert "WHERE status = 'active'" in store.CUSTOM_AGENTS_INDEX_DDL
    assert "UNIQUE INDEX" in store.CUSTOM_AGENTS_INDEX_DDL
    assert "(project_id, slug)" in store.CUSTOM_AGENTS_INDEX_DDL


def test_row_to_dict_parses_jsonb_strings_and_tolerates_garbage():
    row = _row(tools=json.dumps(["list_flags"]), requires="not-json")
    out = store._row_to_dict(row)
    assert out["tools"] == ["list_flags"]
    # Malformed JSONB degrades to [] instead of raising.
    assert out["requires"] == []


def test_row_to_dict_normalizes_legacy_tool_dicts_to_names():
    # Rows written before the agentic rework stored {"tool", "params"} dicts;
    # the params are obsolete but the selection survives as allowed names.
    row = _row(
        tools=json.dumps(
            [{"tool": "list_flags", "params": {}}, "discover_events", {"bogus": 1}]
        )
    )
    out = store._row_to_dict(row)
    assert out["tools"] == ["list_flags", "discover_events"]


@pytest.mark.asyncio
async def test_create_serializes_jsonb_fields_as_strings():
    pool = _FakePool()
    pool.conn.fetchrow_result = _row()

    await store.create_custom_agent(pool, "demo", _definition())

    query, args = pool.conn.executed[0]
    assert "INSERT INTO custom_agents" in query
    assert "$9::jsonb" in query and "$10::jsonb" in query
    # asyncpg needs JSON *strings* for ::jsonb parameters, never raw lists.
    tools_arg = args[8]  # $9::jsonb
    requires_arg = args[9]  # $10::jsonb
    assert isinstance(tools_arg, str) and isinstance(requires_arg, str)
    assert json.loads(tools_arg) == ["discover_events", "query_events"]
    assert json.loads(requires_arg) == []


@pytest.mark.asyncio
async def test_fetch_active_by_slugs_short_circuits_on_empty():
    pool = _FakePool()
    assert await store.fetch_active_by_slugs(pool, "demo", []) == {}
    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_fetch_active_by_slugs_maps_by_slug():
    pool = _FakePool()
    pool.conn.fetch_result = [_row()]
    out = await store.fetch_active_by_slugs(pool, "demo", ["churn_watch"])
    assert set(out) == {"churn_watch"}
    assert out["churn_watch"]["produces"] == "churn_signals"


@pytest.mark.asyncio
async def test_archive_reports_whether_a_row_was_archived():
    pool = _FakePool()
    assert await store.archive_custom_agent(pool, "agent-1") is True
    query, _ = pool.conn.executed[0]
    assert "SET status = 'archived'" in query
    assert "status = 'active'" in query  # archiving an archived row is a no-op
