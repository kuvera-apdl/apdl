"""Supervisor resolution of custom agents: ordering, persistence, resume."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.framework import base as framework_base
from app.framework import tool_catalog
from app.graphs import supervisor


def _definition(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Data: {tool_results}",
        "model_tier": "fast",
        "tools": [],
        "requires": [],
        "produces": "churn_signals",
        "parse_as": "list",
        "memory_query": None,
        "memory_top_k": 5,
        "pipeline_order": 15,
    }
    base.update(overrides)
    return base


class _FakeConn:
    def __init__(self, prior: list[dict] | None = None) -> None:
        self.prior = prior or []
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any) -> int:
        # Audit entries go through fetchval (RETURNING id) — record them too.
        self.executed.append((query, args))
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
    def __init__(self, prior: list[dict] | None = None) -> None:
        self.conn = _FakeConn(prior)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class _BuiltinAgent:
    def __init__(self, name: str, order: int, produces: str) -> None:
        self.name = name
        self.order = order
        self.requires: tuple = ()
        self.produces = produces
        self.ran_at: int | None = None

    def requirements_met(self, state: dict) -> bool:
        return True

    async def run(self, ctx: Any, state: dict) -> Any:
        self.ran_at = _tick()
        return SimpleNamespace(output=[{"finding": "drop"}], metadata={})


_COUNTER = {"n": 0}


def _tick() -> int:
    _COUNTER["n"] += 1
    return _COUNTER["n"]


def _stub_registry(monkeypatch, builtin: _BuiltinAgent) -> None:
    registry = {builtin.name: builtin}
    monkeypatch.setattr(supervisor, "is_registered", lambda name: name in registry)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: registry[name])


def _stub_custom_lookup(monkeypatch, defs: dict[str, dict]) -> None:
    async def fake_fetch(pool, project_id, slugs):
        return {slug: defs[slug] for slug in slugs if slug in defs}

    monkeypatch.setattr(supervisor, "fetch_active_by_slugs", fake_fetch)


@pytest.mark.asyncio
async def test_custom_agent_runs_in_pipeline_order_and_persists(monkeypatch):
    builtin = _BuiltinAgent("behavior_analysis", 10, "insights")
    _stub_registry(monkeypatch, builtin)
    _stub_custom_lookup(monkeypatch, {"churn_watch": _definition(pipeline_order=15)})

    llm_calls: list[str] = []

    async def fake_chat(model_tier, messages, **kwargs):
        llm_calls.append(model_tier)
        return '[{"signal": "activation drop"}]'

    monkeypatch.setattr(framework_base, "chat_completion", fake_chat)

    custom_ran_at: dict[str, int] = {}

    async def fake_run_tool(ctx, name, params):
        return {}

    monkeypatch.setattr(tool_catalog, "run_tool", fake_run_tool)

    original_build = supervisor.CustomAgent.build_prompt

    def tracking_build(self, ctx, state, working):
        custom_ran_at["at"] = _tick()
        return original_build(self, ctx, state, working)

    monkeypatch.setattr(supervisor.CustomAgent, "build_prompt", tracking_build)

    pool = _FakePool()
    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-1",
        project_id="demo",
        analysis_types=["churn_watch", "behavior_analysis"],
        time_range_days=7,
        autonomy_level=2,
    )

    # pipeline_order 15 places the custom agent after behavior_analysis (10).
    assert builtin.ran_at is not None and builtin.ran_at < custom_ran_at["at"]
    assert llm_calls == ["fast"]

    persisted = [
        (query, args)
        for query, args in pool.conn.executed
        if "INSERT INTO agent_run_results" in query
    ]
    custom_persist = next(args for _, args in persisted if args[1] == "churn_watch")
    assert custom_persist[2] == "churn_signals"
    assert json.loads(custom_persist[3]) == [{"signal": "activation drop"}]

    statuses = [args[1] for query, args in pool.conn.executed if "UPDATE agent_runs" in query]
    assert statuses[-1] == "completed"


@pytest.mark.asyncio
async def test_unknown_slug_is_error_not_crash(monkeypatch):
    builtin = _BuiltinAgent("behavior_analysis", 10, "insights")
    _stub_registry(monkeypatch, builtin)
    _stub_custom_lookup(monkeypatch, {})  # nothing resolves

    pool = _FakePool()
    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-2",
        project_id="demo",
        analysis_types=["behavior_analysis", "ghost_agent"],
        time_range_days=7,
        autonomy_level=2,
    )

    statuses = [args[1] for query, args in pool.conn.executed if "UPDATE agent_runs" in query]
    assert statuses[-1] == "completed_with_errors"


@pytest.mark.asyncio
async def test_custom_agent_with_unmet_requires_is_skipped(monkeypatch):
    builtin = _BuiltinAgent("behavior_analysis", 10, "insights")
    _stub_registry(monkeypatch, builtin)
    _stub_custom_lookup(
        monkeypatch,
        {"churn_watch": _definition(requires=["experiment_designs"], pipeline_order=30)},
    )

    async def fail_chat(*args, **kwargs):  # custom agent must never reach the LLM
        raise AssertionError("skipped agent must not call the LLM")

    monkeypatch.setattr(framework_base, "chat_completion", fail_chat)

    pool = _FakePool()
    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-3",
        project_id="demo",
        analysis_types=["churn_watch"],
        time_range_days=7,
        autonomy_level=2,
    )

    skipped_audits = [
        args
        for query, args in pool.conn.executed
        if "INSERT INTO agent_audit_log" in query and args[1] == "churn_watch_skipped"
    ]
    assert skipped_audits, "unmet requires must produce an audited skip"


@pytest.mark.asyncio
async def test_resume_skips_completed_custom_agent(monkeypatch):
    builtin = _BuiltinAgent("behavior_analysis", 10, "insights")
    _stub_registry(monkeypatch, builtin)
    definition = _definition(pipeline_order=15)
    _stub_custom_lookup(monkeypatch, {"churn_watch": definition})

    async def fail_chat(*args, **kwargs):
        raise AssertionError("completed custom agent must not re-run on resume")

    monkeypatch.setattr(framework_base, "chat_completion", fail_chat)

    prior = [
        {
            "agent_name": "behavior_analysis",
            "produces": "insights",
            "output": json.dumps([{"finding": "drop"}]),
        },
        {
            "agent_name": "churn_watch",
            "produces": "churn_signals",
            "output": json.dumps([{"signal": "x"}]),
        },
    ]
    pool = _FakePool(prior)
    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-4",
        project_id="demo",
        analysis_types=["behavior_analysis", "churn_watch"],
        time_range_days=7,
        autonomy_level=2,
        resume=True,
    )

    assert builtin.ran_at is None
    statuses = [args[1] for query, args in pool.conn.executed if "UPDATE agent_runs" in query]
    assert statuses[-1] == "completed"
