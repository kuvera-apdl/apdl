"""Tests for the run introspection endpoints (admin-plan gaps G1–G3).

Uses ASGITransport (no lifespan) with a fake pool injected on app.state, the
same pattern as the other router tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.graphs.supervisor import _persist_results
from app.main import app


def _run_row(run_id: str, project_id: str = "demo", status: str = "completed") -> dict:
    return {
        "run_id": run_id,
        "project_id": project_id,
        "status": status,
        "phase": "done",
        "insights_count": 2,
        "experiments_count": 1,
        "started_at": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 10, 12, 5, tzinfo=timezone.utc),
        "trigger_type": "manual",
        "autonomy_level": 2,
        "config": json.dumps(
            {"analysis_types": ["behavior_analysis", "experiment_design"]}
        ),
    }


class FakeConn:
    def __init__(self, store: dict) -> None:
        self.store = store
        self.executes: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        if "FROM agent_runs" in query:
            rows = [r for r in self.store["runs"] if r["project_id"] == args[0]]
            if "status = $2" in query:
                rows = [r for r in rows if r["status"] == args[1]]
            return rows[: args[-1]]
        if "FROM agent_run_results" in query:
            return [r for r in self.store["results"] if r["run_id"] == args[0]]
        if "FROM agent_audit_log" in query:
            return [r for r in self.store["audit"] if r["run_id"] == args[0]][: args[1]]
        raise AssertionError(f"Unexpected fetch: {query}")

    async def fetchval(self, query: str, *args):
        if "SELECT 1 FROM agent_runs" in query:
            return (
                1
                if any(
                    r["run_id"] == args[0] and r["project_id"] == args[1]
                    for r in self.store["runs"]
                )
                else None
            )
        raise AssertionError(f"Unexpected fetchval: {query}")

    async def execute(self, query: str, *args):
        self.executes.append((query, args))


class _Acquire:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *exc) -> bool:
        return False


class FakePool:
    def __init__(self, store: dict) -> None:
        self.conn = FakeConn(store)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _client(store: dict) -> AsyncClient:
    app.state.pg_pool = FakePool(store)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


STORE = {
    "runs": [
        _run_row("run-1", status="completed"),
        _run_row("run-2", status="running"),
        _run_row("run-other", project_id="other"),
    ],
    "results": [
        {
            "run_id": "run-1",
            "produces": "insights",
            "output": json.dumps(
                [{"title": "Drop-off on checkout", "severity": "high"}]
            ),
        },
        {
            "run_id": "run-1",
            "produces": "experiment_designs",
            "output": json.dumps([{"hypothesis": "Bigger CTA converts"}]),
        },
        {
            "run_id": "run-1",
            "produces": "churn_signals",
            "output": json.dumps([{"signal": "activation drop"}]),
        },
    ],
    "audit": [
        {
            "id": 2,
            "run_id": "run-1",
            "action_type": "behavior_analysis_complete",
            "config": json.dumps({"produced": "insights", "count": 2}),
            "safety_result": json.dumps({}),
            "approval_status": None,
            "created_at": datetime(2026, 6, 10, 12, 4, tzinfo=timezone.utc),
        },
        {
            "id": 1,
            "run_id": "run-1",
            "action_type": "supervisor_start",
            "config": json.dumps({"autonomy_level": 2}),
            "safety_result": json.dumps({}),
            "approval_status": None,
            "created_at": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        },
    ],
}


@pytest.mark.asyncio
async def test_list_runs_filters_by_project_and_status():
    async with _client(STORE) as client:
        resp = await client.get("/v1/agents/runs", params={"project_id": "demo"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert [r["run_id"] for r in body["runs"]] == ["run-1", "run-2"]
        assert body["runs"][0]["started_at"] == "2026-06-10T12:00:00+00:00"
        # Requested agents surface from config so clients need no local record.
        assert body["runs"][0]["analysis_types"] == [
            "behavior_analysis",
            "experiment_design",
        ]
        assert body["runs"][0]["autonomy_level"] == 2

        filtered = await client.get(
            "/v1/agents/runs", params={"project_id": "demo", "status": "running"}
        )
        assert [r["run_id"] for r in filtered.json()["runs"]] == ["run-2"]


@pytest.mark.asyncio
async def test_list_runs_requires_project_id():
    async with _client(STORE) as client:
        resp = await client.get("/v1/agents/runs")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_results_aggregates_by_produces_with_empty_defaults():
    async with _client(STORE) as client:
        resp = await client.get("/v1/agents/run-1/results")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "run-1"
        assert body["insights"] == [
            {"title": "Drop-off on checkout", "severity": "high"}
        ]
        assert body["experiment_designs"] == [{"hypothesis": "Bigger CTA converts"}]
        assert body["personalizations"] == []
        assert body["feature_proposals"] == []
        # A custom agent's produces key is surfaced, not silently dropped.
        assert body["custom_outputs"] == {
            "churn_signals": [{"signal": "activation drop"}]
        }


@pytest.mark.asyncio
async def test_run_results_404_for_unknown_run():
    async with _client(STORE) as client:
        resp = await client.get("/v1/agents/nope/results")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_results_hide_other_tenant_run():
    async with _client(STORE) as client:
        resp = await client.get("/v1/agents/run-other/results")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_audit_returns_parsed_entries():
    async with _client(STORE) as client:
        resp = await client.get("/v1/agents/run-1/audit")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        first = body["audit"][0]
        assert first["action_type"] == "behavior_analysis_complete"
        assert first["config"] == {"produced": "insights", "count": 2}
        assert first["created_at"].startswith("2026-06-10T12:04")

        missing = await client.get("/v1/agents/nope/audit")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_persist_results_upserts_jsonb():
    pool = FakePool({"runs": [], "results": [], "audit": []})
    await _persist_results(pool, "run-9", "behavior_analysis", "insights", [{"a": 1}])
    query, args = pool.conn.executes[0]
    assert "INSERT INTO agent_run_results" in query
    assert "ON CONFLICT (run_id, agent_name)" in query
    assert args[0] == "run-9"
    assert args[1] == "behavior_analysis"
    assert args[2] == "insights"
    assert json.loads(args[3]) == [{"a": 1}]


@pytest.mark.asyncio
async def test_persist_results_never_raises():
    class ExplodingPool:
        def acquire(self):
            raise RuntimeError("boom")

    # Must swallow the failure — persistence cannot kill a run.
    await _persist_results(ExplodingPool(), "run-9", "x", "insights", [])
