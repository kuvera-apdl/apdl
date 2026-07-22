"""Tests for the run introspection endpoints (admin-plan gaps G1–G3).

Uses ASGITransport (no lifespan) with a fake pool injected on app.state, the
same pattern as the other router tests.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.graphs.supervisor import RunResultPersistenceError, _persist_results
from app.main import app
from app.store.run_leases import RunLeaseLostError


def _run_row(run_id: str, project_id: str = "demo", status: str = "completed") -> dict:
    terminal = status in {
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
        "manual_intervention",
    }
    return {
        "run_id": run_id,
        "project_id": project_id,
        "status": status,
        "phase": "done",
        "execution_lane_project_id": None if terminal else project_id,
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
        if "FROM agent_approval_effects AS effect" in query:
            return [
                effect
                for effect in self.store.get("effects", [])
                if effect["run_id"] == args[0]
                and effect["project_id"] == args[1]
                and effect["status"] in {"queued", "processing", "retryable_failed"}
            ]
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

    async def fetchrow(self, query: str, *args):
        if "SELECT status, phase" in query and "FROM agent_runs" in query:
            return next(
                (
                    {
                        "status": row["status"],
                        "phase": row["phase"],
                        "execution_lane_project_id": row["execution_lane_project_id"],
                    }
                    for row in self.store["runs"]
                    if row["run_id"] == args[0] and row["project_id"] == args[1]
                ),
                None,
            )
        raise AssertionError(f"Unexpected fetchrow: {query}")

    def transaction(self):
        return _Transaction()

    async def execute(self, query: str, *args):
        self.executes.append((query, args))
        if (
            "UPDATE agent_approval_effects AS effect" in query
            and "effect.status = 'queued'" in query
        ):
            for effect in self.store.get("effects", []):
                if (
                    effect["run_id"] == args[0]
                    and effect["project_id"] == args[1]
                    and effect["status"] == "queued"
                ):
                    effect["status"] = "manual_intervention"
        if "UPDATE agent_runs" in query and "SET status = $3" in query:
            for row in self.store["runs"]:
                if row["run_id"] == args[0] and row["project_id"] == args[1]:
                    row.update(
                        status=args[2],
                        phase=args[3],
                        execution_lane_project_id=(
                            None if args[2] == "cancelled" else row["project_id"]
                        ),
                    )
        return "UPDATE 1"


class _Transaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc) -> bool:
        return False


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
    "effects": [],
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
async def test_cancel_run_is_tenant_scoped_audited_and_idempotent():
    store = copy.deepcopy(STORE)
    async with _client(store) as client:
        response = await client.post("/v1/agents/run-2/cancel")
        replay = await client.post("/v1/agents/run-2/cancel")
        hidden = await client.post("/v1/agents/run-other/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-2",
        "previous_status": "running",
        "status": "cancelled",
    }
    assert replay.status_code == 200
    assert replay.json()["previous_status"] == "cancelled"
    assert hidden.status_code == 404
    assert (
        next(row for row in store["runs"] if row["run_id"] == "run-2")["status"]
        == "cancelled"
    )

    pool = app.state.pg_pool
    audit_calls = [
        args
        for query, args in pool.conn.executes
        if "INSERT INTO agent_audit_log" in query
    ]
    assert len(audit_calls) == 1
    assert json.loads(audit_calls[0][1]) == {
        "actor_credential_id": "test-agents",
        "actor_user_id": None,
        "previous_phase": "done",
        "previous_status": "running",
    }
    assert audit_calls[0][3] == "run-cancelled:run-2"
    effect_query = next(
        query
        for query, _ in pool.conn.executes
        if "UPDATE agent_approval_effects AS effect" in query
    )
    assert "effect.status = 'queued'" in effect_query


@pytest.mark.asyncio
async def test_cancel_run_retains_lane_while_claimed_effect_is_draining():
    store = copy.deepcopy(STORE)
    store["effects"] = [
        {
            "effect_id": "22222222-2222-4222-8222-222222222222",
            "run_id": "run-2",
            "project_id": "demo",
            "status": "processing",
        }
    ]

    async with _client(store) as client:
        response = await client.post("/v1/agents/run-2/cancel")
        replay = await client.post("/v1/agents/run-2/cancel")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-2",
        "previous_status": "running",
        "status": "cancelling",
    }
    assert replay.json() == {
        "run_id": "run-2",
        "previous_status": "cancelling",
        "status": "cancelling",
    }
    run = next(row for row in store["runs"] if row["run_id"] == "run-2")
    assert run["phase"] == "cancellation_draining"
    assert run["execution_lane_project_id"] == "demo"
    assert store["effects"][0]["status"] == "processing"

    pool = app.state.pg_pool
    assert not any(
        "UPDATE feature_proposals" in query for query, _ in pool.conn.executes
    )
    pending_audits = [
        args
        for query, args in pool.conn.executes
        if "INSERT INTO agent_audit_log" in query
    ]
    assert all(args[3] == "run-cancellation-requested:run-2" for args in pending_audits)
    assert all(args[5] == "cancelling" for args in pending_audits)


@pytest.mark.asyncio
async def test_cancel_run_rejects_an_already_completed_run():
    store = copy.deepcopy(STORE)
    async with _client(store) as client:
        response = await client.post("/v1/agents/run-1/cancel")

    assert response.status_code == 409
    assert "already terminal" in response.json()["detail"]


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
    await _persist_results(
        pool,
        "run-9",
        "behavior_analysis",
        "insights",
        [{"a": 1}],
        {"source": "test"},
        "worker-9",
    )
    query, args = pool.conn.executes[0]
    assert "FROM agent_runs" in query and "FOR UPDATE" in query
    assert "execution_lane_project_id = project_id" in query
    assert "INSERT INTO agent_run_results" in query
    assert "ON CONFLICT (run_id, agent_name)" in query
    assert args[0] == "run-9"
    assert args[1] == "behavior_analysis"
    assert args[2] == "insights"
    assert json.loads(args[3]) == [{"a": 1}]
    assert json.loads(args[4]) == {"source": "test"}
    assert args[5] == "worker-9"


@pytest.mark.asyncio
async def test_persist_results_fails_closed():
    class ExplodingPool:
        def acquire(self):
            raise RuntimeError("boom")

    with pytest.raises(RunResultPersistenceError):
        await _persist_results(
            ExplodingPool(), "run-9", "x", "insights", [], {}, "worker-9"
        )


@pytest.mark.asyncio
async def test_persist_results_fences_a_stale_owner():
    pool = FakePool({"runs": [], "results": [], "audit": []})

    async def stale_execute(query: str, *args):
        return "INSERT 0 0"

    pool.conn.execute = stale_execute

    with pytest.raises(RunLeaseLostError):
        await _persist_results(pool, "run-9", "x", "insights", [], {}, "worker-stale")
