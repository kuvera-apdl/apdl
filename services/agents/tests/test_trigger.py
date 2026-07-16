"""The trigger endpoint starts a run and stores config as JSONB."""

from __future__ import annotations

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
async def test_self_registered_overprivileged_project_cannot_start_run(monkeypatch):
    async def authenticate_self_registered(request: Request):
        principal = Principal(
            credential_id="self-registered",
            project_id="demo",
            roles=frozenset(
                {"agents:read", "agents:run", "agents:manage", "agents:approve"}
            ),
            self_registered_project=True,
        )
        request.state.principal = principal
        return principal

    supervisor_calls: list[dict[str, Any]] = []

    async def forbidden_supervisor(**kwargs: Any):
        supervisor_calls.append(kwargs)

    app.dependency_overrides[authenticate_request] = authenticate_self_registered
    monkeypatch.setattr(triggers, "run_supervisor", forbidden_supervisor)
    pool = _FakePool()

    async with _client(pool) as client:
        response = await client.post(
            "/v1/agents/trigger",
            json={"project_id": "demo", "trigger_type": "manual"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Agents execution is unavailable for self-registered projects"
    )
    assert pool.conn.executed == []
    assert supervisor_calls == []


@pytest.mark.asyncio
async def test_trigger_starts_run_and_serializes_config(monkeypatch):
    kicked: list = []

    async def fake_supervisor(**kwargs):
        kicked.append(kwargs)

    monkeypatch.setattr(triggers, "run_supervisor", fake_supervisor)
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
    assert "lease_expires_at" in query
    assert "now() + ($5 * interval '1 second')" in query
    assert "$6::jsonb" in query
    assert args[-2] == triggers.RUN_LEASE_SECONDS
    config_arg = args[-1]
    assert isinstance(config_arg, str)
    assert json.loads(config_arg) == {
        "analysis_types": ["behavior_analysis", "experiment_design"],
        "time_range_days": 7,
    }

    # Supervisor is launched as a background task for the same run.
    assert kicked and kicked[0]["analysis_types"] == [
        "behavior_analysis",
        "experiment_design",
    ]
    assert kicked[0]["run_id"] == body["run_id"]


@pytest.mark.asyncio
async def test_trigger_defaults_apply(monkeypatch):
    async def fake_supervisor(**kwargs):
        return None

    monkeypatch.setattr(triggers, "run_supervisor", fake_supervisor)
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
async def test_trigger_accepts_active_custom_agent_slug(monkeypatch):
    kicked: list = []

    async def fake_supervisor(**kwargs):
        kicked.append(kwargs)

    monkeypatch.setattr(triggers, "run_supervisor", fake_supervisor)
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
    assert kicked[0]["analysis_types"] == ["behavior_analysis", "churn_watch"]


@pytest.mark.asyncio
async def test_trigger_rejects_slug_not_active_in_project(monkeypatch):
    async def fake_supervisor(**kwargs):
        raise AssertionError("supervisor must not start for an unknown agent")

    monkeypatch.setattr(triggers, "run_supervisor", fake_supervisor)
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
async def test_trigger_rejects_disabled_builtin(monkeypatch, disabled):
    """Disabled built-ins are rejected before a run or job is created."""

    async def fake_supervisor(**kwargs):
        raise AssertionError("supervisor must not start for a disabled agent")

    monkeypatch.setattr(triggers, "run_supervisor", fake_supervisor)
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
