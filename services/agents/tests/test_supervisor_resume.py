"""On resume the supervisor reloads prior results, skips already-completed
agents, and runs only the not-yet-run agents — closing the single-run loop."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.graphs import supervisor


class _FakeConn:
    def __init__(self, prior: list[dict]) -> None:
        self.prior = prior
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any) -> int:
        return 1

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        if "FROM agent_run_results" in query:
            return self.prior
        return []


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, prior: list[dict]) -> None:
        self.conn = _FakeConn(prior)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class _Agent:
    def __init__(self, name: str, order: int, produces: str) -> None:
        self.name = name
        self.order = order
        self.requires: tuple = ()
        self.produces = produces
        self.ran = False

    def requirements_met(self, state: dict) -> bool:
        return True

    async def run(self, ctx: Any, state: dict) -> Any:
        self.ran = True
        return SimpleNamespace(output=[{"id": self.name}], metadata={})


@pytest.mark.asyncio
async def test_resume_skips_completed_and_runs_remaining(monkeypatch) -> None:
    done = _Agent("experiment_design", 20, "experiment_designs")
    later = _Agent("feature_proposal", 40, "feature_proposals")
    registry = {"experiment_design": done, "feature_proposal": later}
    monkeypatch.setattr(supervisor, "is_registered", lambda name: name in registry)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: registry[name])

    prior = [
        {
            "agent_name": "experiment_design",
                "produces": "experiment_designs",
                "output": json.dumps([{"experiment_id": "exp_x"}]),
                "metadata": {},
        }
    ]
    pool = _FakePool(prior)

    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["experiment_design", "feature_proposal"],
        time_range_days=7,
        autonomy_level=2,
        resume=True,
    )

    assert done.ran is False, "already-completed agent must be skipped on resume"
    assert later.ran is True, "the not-yet-run agent must run on resume"
    statuses = [args[1] for (query, args) in pool.conn.executed if "UPDATE agent_runs" in query]
    assert "completed" in statuses  # the run finishes after the last agent runs


@pytest.mark.asyncio
async def test_resume_finishes_with_errors_after_approved_deploy_failure(monkeypatch) -> None:
    done = _Agent("experiment_design", 20, "experiment_designs")
    registry = {"experiment_design": done}
    monkeypatch.setattr(supervisor, "is_registered", lambda name: name in registry)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: registry[name])

    deploy_error = "experiment deploy failed: exp_failed"
    prior = [
        {
            "agent_name": "experiment_design",
                "produces": "experiment_designs",
                "output": json.dumps([{"experiment_id": "exp_failed"}]),
                "metadata": {},
        },
        {
            "agent_name": "approval_errors",
                "produces": "errors",
                "output": json.dumps([deploy_error]),
                "metadata": {},
        },
    ]
    pool = _FakePool(prior)

    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["experiment_design"],
        time_range_days=7,
        autonomy_level=2,
        resume=True,
    )

    assert done.ran is False
    statuses = [
        args[1]
        for query, args in pool.conn.executed
        if "UPDATE agent_runs" in query
    ]
    assert "completed_with_errors" in statuses
    assert "completed" not in statuses


@pytest.mark.asyncio
async def test_recovered_run_restores_persisted_pending_gate(monkeypatch) -> None:
    gated = _Agent("experiment_design", 20, "experiment_designs")
    monkeypatch.setattr(supervisor, "is_registered", lambda name: True)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: gated)
    pool = _FakePool(
        [
            {
                "agent_name": "experiment_design",
                "produces": "experiment_designs",
                "output": [{"experiment_id": "exp_x"}],
                "metadata": {
                    "needs_approval": True,
                    "approval_gate": {
                        "gate_id": "run-1:experiment_design",
                        "agent_name": "experiment_design",
                        "produces": "experiment_designs",
                        "phase": "experiment_design_approval",
                        "state": "pending",
                    },
                },
            }
        ]
    )

    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["experiment_design"],
        time_range_days=7,
        autonomy_level=2,
        resume=True,
    )

    assert gated.ran is False
    transitions = [
        args[1:3]
        for query, args in pool.conn.executed
        if "UPDATE agent_runs" in query and "SET status = $2" in query
    ]
    assert ("waiting_approval", "experiment_design_approval") in transitions
    assert not any(status == "completed" for status, _ in transitions)


@pytest.mark.asyncio
async def test_post_result_bookkeeping_runs_after_durable_result(monkeypatch) -> None:
    pool = _FakePool([])
    ordering: list[str] = []

    class _OrderedAgent(_Agent):
        async def after_result_persisted(self, ctx, state, result) -> None:
            assert any(
                "INSERT INTO agent_run_results" in query
                for query, _ in pool.conn.executed
            )
            ordering.append("post_persist")

    agent = _OrderedAgent("behavior_analysis", 10, "insights")
    monkeypatch.setattr(supervisor, "is_registered", lambda name: True)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: agent)

    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["behavior_analysis"],
        time_range_days=7,
        autonomy_level=2,
    )

    assert ordering == ["post_persist"]
