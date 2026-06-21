"""The approval endpoint enqueues approved proposals and kicks an implement run."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import approvals

_PROPOSAL = {
    "proposal_id": "p1",
    "title": "Add dark mode",
    "spec": "Implement a dark-mode toggle across the app.",
}


class _FakeConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args: Any):
        if "FROM agent_runs" in query:
            return self.store["run"]
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any):
        if "FROM agent_run_results" in query:
            return self.store["results"]
        raise AssertionError(f"Unexpected fetch: {query}")

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, store: dict[str, Any]) -> None:
        self.conn = _FakeConn(store)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _run_row(status: str = "waiting_approval", level: int = 3) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "status": status,
        "phase": "feature_proposal_approval",
        "project_id": "demo",
        "autonomy_level": level,
    }


def _patch(monkeypatch):
    enq: list = []
    kicked: list = []

    async def fake_enqueue(pool, run_id, project_id, proposals):
        enq.append((run_id, project_id, proposals))
        return len(proposals)

    async def fake_supervisor(**kwargs):
        kicked.append(kwargs)

    monkeypatch.setattr(approvals, "enqueue_proposals", fake_enqueue)
    monkeypatch.setattr(approvals, "run_supervisor", fake_supervisor)
    return enq, kicked


def _client(store: dict[str, Any]) -> AsyncClient:
    app.state.pg_pool = _FakePool(store)
    app.state.vector_store = object()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_approval_enqueues_and_kicks_implementation(monkeypatch):
    enq, kicked = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert enq and enq[0][1] == "demo"
    assert enq[0][2][0]["proposal_id"] == "p1"
    assert kicked and kicked[0]["analysis_types"] == ["code_implementation"]
    assert kicked[0]["project_id"] == "demo"
    assert kicked[0]["autonomy_level"] == 3

    # The kicked run's config must be a JSON *string* for the jsonb column;
    # a raw dict makes asyncpg raise "expected str, got dict".
    _, run_insert_args = next(
        (q, a) for q, a in app.state.pg_pool.conn.executed if "INSERT INTO agent_runs" in q
    )
    assert isinstance(run_insert_args[-1], str)
    assert json.loads(run_insert_args[-1])["analysis_types"] == ["code_implementation"]


@pytest.mark.asyncio
async def test_approval_without_proposals_does_not_kick(monkeypatch):
    enq, kicked = _patch(monkeypatch)
    store = {"run": _run_row(), "results": []}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert enq == []
    assert kicked == []


@pytest.mark.asyncio
async def test_rejection_never_kicks(monkeypatch):
    enq, kicked = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": False})

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert enq == []
    assert kicked == []
