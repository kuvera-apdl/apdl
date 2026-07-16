"""The supervisor must halt at an approval gate, leaving the run in
waiting_approval — not fall through and mark it completed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.graphs import supervisor
from app.graphs.experiment_design import ExperimentDesignAgent


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any) -> int:
        return 1


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


class _GatingAgent:
    name = "experiment_design"
    order = 20
    requires: tuple = ()
    produces = "experiment_designs"

    def requirements_met(self, state: dict) -> bool:
        return True

    async def run(self, ctx: Any, state: dict) -> Any:
        return SimpleNamespace(
            output=[{"experiment_id": "exp_x"}],
            metadata={"needs_approval": True, "experiment_id": "exp_x"},
        )


class _InvalidExperimentAgent(_GatingAgent):
    async def run(self, ctx: Any, state: dict) -> Any:
        ExperimentDesignAgent().parse('[{"experiment_id":"exp_x","unknown":true}]')


@pytest.mark.asyncio
async def test_supervisor_halts_at_approval_gate(monkeypatch) -> None:
    agent = _GatingAgent()
    monkeypatch.setattr(supervisor, "is_registered", lambda name: True)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: agent)

    pool = _FakePool()
    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["experiment_design"],
        time_range_days=7,
        autonomy_level=2,
    )

    statuses = [args[1] for (query, args) in pool.conn.executed if "UPDATE agent_runs" in query]
    assert "waiting_approval" in statuses
    # Once gated, the run must not be completed by the supervisor.
    assert "completed" not in statuses
    assert "completed_with_errors" not in statuses


@pytest.mark.asyncio
async def test_invalid_experiment_output_uses_supervisor_error_path(monkeypatch) -> None:
    agent = _InvalidExperimentAgent()
    monkeypatch.setattr(supervisor, "is_registered", lambda name: True)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: agent)

    pool = _FakePool()
    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["experiment_design"],
        time_range_days=7,
        autonomy_level=4,
    )

    statuses = [args[1] for (query, args) in pool.conn.executed if "UPDATE agent_runs" in query]
    assert "completed_with_errors" in statuses
    assert "waiting_approval" not in statuses
