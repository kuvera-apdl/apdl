"""The trigger endpoint durably queues a run and stores canonical JSONB config."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import triggers


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.custom_rows: list[dict[str, Any]] = []
        self.transaction_entries = 0
        self.transaction_exits = 0

    def transaction(self) -> "_Transaction":
        return _Transaction(self)

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any):
        # No active run exists — the concurrency guard passes.
        return None

    async def fetch(self, query: str, *args: Any):
        # Custom-agent slug resolution (fetch_active_by_slugs).
        if "FROM custom_agents" in query:
            return self.custom_rows
        return []


class _Transaction:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        self.conn.transaction_entries += 1

    async def __aexit__(self, *exc: Any) -> bool:
        self.conn.transaction_exits += 1
        return False


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
async def test_trigger_denies_cross_tenant_project():
    pool = _FakePool()
    async with _client(pool) as client:
        response = await client.post(
            "/v1/agents/trigger",
            json={"project_id": "other", "trigger_type": "manual"},
        )

    assert response.status_code == 403
    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_trigger_requires_agents_run_role():
    async def authenticate_reader(request: Request):
        principal = Principal(
            credential_id="reader",
            project_id="demo",
            roles=frozenset({"agents:read"}),
            self_registered_project=False,
            execution_authorized=True,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_reader
    pool = _FakePool()
    async with _client(pool) as client:
        response = await client.post(
            "/v1/agents/trigger",
            json={"project_id": "demo", "trigger_type": "manual"},
        )

    assert response.status_code == 403
    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_self_registered_overprivileged_project_cannot_start_run():
    async def authenticate_self_registered(request: Request):
        principal = Principal(
            credential_id="self-registered",
            project_id="demo",
            roles=frozenset(
                {"agents:read", "agents:run", "agents:manage", "agents:approve"}
            ),
            self_registered_project=True,
            execution_authorized=False,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_self_registered
    pool = _FakePool()

    async with _client(pool) as client:
        response = await client.post(
            "/v1/agents/trigger",
            json={"project_id": "demo", "trigger_type": "manual"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Agents execution requires operator project authorization"
    )
    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_trigger_starts_run_and_serializes_config():
    pool = _FakePool()

    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/trigger",
            json={
                "project_id": "demo",
                "trigger_type": "manual",
                "analysis_types": ["behavior_analysis", "experiment_design"],
                "time_range_days": 7,
                "autonomy_level": 3,
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["run_id"]

    # The agent_runs insert must hand asyncpg a JSON *string* for the jsonb
    # config column, never a raw dict — asyncpg rejects a dict with
    # "invalid input for query argument ... (expected str, got dict)".
    query, args = next(
        (q, a) for q, a in pool.conn.executed if "INSERT INTO agent_runs" in q
    )
    advisory_index = next(
        index
        for index, (statement, _) in enumerate(pool.conn.executed)
        if "pg_advisory_xact_lock" in statement
    )
    insert_index = next(
        index
        for index, (statement, _) in enumerate(pool.conn.executed)
        if "INSERT INTO agent_runs" in statement
    )
    assert advisory_index < insert_index
    assert pool.conn.transaction_entries == pool.conn.transaction_exits == 1
    assert "lease_owner_id, lease_expires_at" in query
    assert "NULL, NULL" in query
    assert "$5::jsonb" in query
    config_arg = args[-1]
    assert isinstance(config_arg, str)
    assert json.loads(config_arg) == {
        "analysis_types": ["behavior_analysis", "experiment_design"],
        "time_range_days": 7,
    }

    # The request only commits a queue row. Replica dispatchers execute it.
    assert "run_supervisor" not in triggers.__dict__


class _ConcurrentRunState:
    def __init__(self) -> None:
        self.advisory_lock = asyncio.Lock()
        self.active_run_id: str | None = None
        self.insert_count = 0


class _ConcurrentTransaction:
    def __init__(self, conn: "_ConcurrentConn") -> None:
        self.conn = conn

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        if self.conn.lock_held:
            self.conn.state.advisory_lock.release()
            self.conn.lock_held = False
        return False


class _ConcurrentConn(_FakeConn):
    def __init__(self, state: _ConcurrentRunState) -> None:
        super().__init__()
        self.state = state
        self.lock_held = False

    def transaction(self) -> _ConcurrentTransaction:
        return _ConcurrentTransaction(self)

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))
        if "pg_advisory_xact_lock" in query:
            await self.state.advisory_lock.acquire()
            self.lock_held = True
        elif "INSERT INTO agent_runs" in query:
            self.state.active_run_id = str(args[0])
            self.state.insert_count += 1

    async def fetchval(self, query: str, *args: Any):
        if "SELECT run_id FROM agent_runs" in query:
            return self.state.active_run_id
        return await super().fetchval(query, *args)


class _ConcurrentAcquire:
    def __init__(self, pool: "_ConcurrentPool") -> None:
        self.pool = pool
        self.conn: _ConcurrentConn | None = None

    async def __aenter__(self) -> _ConcurrentConn:
        self.conn = _ConcurrentConn(self.pool.state)
        self.pool.connections.append(self.conn)
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _ConcurrentPool:
    def __init__(self) -> None:
        self.state = _ConcurrentRunState()
        self.connections: list[_ConcurrentConn] = []

    def acquire(self) -> _ConcurrentAcquire:
        return _ConcurrentAcquire(self)


@pytest.mark.asyncio
async def test_concurrent_trigger_requests_create_only_one_active_run():
    pool = _ConcurrentPool()

    async with _client(pool) as client:
        responses = await asyncio.gather(
            client.post(
                "/v1/agents/trigger",
                json={"project_id": "demo", "trigger_type": "manual"},
            ),
            client.post(
                "/v1/agents/trigger",
                json={"project_id": "demo", "trigger_type": "manual"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert pool.state.insert_count == 1
    assert pool.state.active_run_id is not None


@pytest.mark.asyncio
async def test_trigger_defaults_apply():
    pool = _FakePool()

    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/trigger",
            json={"project_id": "demo", "trigger_type": "scheduled"},
        )

    assert resp.status_code == 200
    _, args = next(
        (q, a) for q, a in pool.conn.executed if "INSERT INTO agent_runs" in q
    )
    assert json.loads(args[-1]) == {
        "analysis_types": ["behavior_analysis"],
        "time_range_days": 7,
    }


def _custom_row(slug: str, project_id: str = "demo") -> dict[str, Any]:
    return {
        "agent_id": "agent-1",
        "project_id": project_id,
        "slug": slug,
        "display_name": slug,
        "description": "",
        "system_prompt": "s",
        "user_prompt_template": "u",
        "model_tier": "fast",
        "tools": "[]",
        "requires": "[]",
        "produces": "custom_out",
        "memory_query": None,
        "memory_top_k": 5,
        "pipeline_order": 60,
        "status": "active",
        "created_at": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_trigger_accepts_active_custom_agent_slug():
    pool = _FakePool()
    pool.conn.custom_rows = [_custom_row("churn_watch")]

    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/trigger",
            json={
                "project_id": "demo",
                "trigger_type": "manual",
                "analysis_types": ["behavior_analysis", "churn_watch"],
            },
        )

    assert resp.status_code == 200
    _, args = next(
        (q, a) for q, a in pool.conn.executed if "INSERT INTO agent_runs" in q
    )
    assert json.loads(args[-1])["analysis_types"] == [
        "behavior_analysis",
        "churn_watch",
    ]


@pytest.mark.asyncio
async def test_trigger_rejects_slug_not_active_in_project():
    pool = _FakePool()  # custom_rows empty: the slug resolves to nothing

    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/trigger",
            json={
                "project_id": "demo",
                "trigger_type": "manual",
                "analysis_types": ["ghost_agent"],
            },
        )

    assert resp.status_code == 422
    assert "ghost_agent" in resp.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", ["personalization", "experiment_evaluation"])
async def test_trigger_rejects_disabled_builtin(disabled):
    """Disabled built-ins are rejected before a run or job is created."""

    pool = _FakePool()

    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/trigger",
            json={
                "project_id": "demo",
                "trigger_type": "manual",
                "analysis_types": ["behavior_analysis", disabled],
            },
        )

    assert resp.status_code == 422
    assert disabled in resp.json()["detail"]
    assert pool.conn.executed == []


@pytest.mark.asyncio
async def test_trigger_rejects_removed_target_experiment_contract():
    pool = _FakePool()

    async with _client(pool) as client:
        resp = await client.post(
            "/v1/agents/trigger",
            json={
                "project_id": "demo",
                "trigger_type": "manual",
                "analysis_types": ["behavior_analysis"],
                "target_experiment_id": "exp_checkout",
            },
        )

    assert resp.status_code == 422
    assert pool.conn.executed == []
