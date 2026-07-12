"""Phase 3: experiment_evaluation — maturity gate, verdict execution, manual scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs import experiment_evaluation
from app.graphs.experiment_evaluation import ExperimentEvaluationAgent


class _FakeAudit:
    def __init__(self) -> None:
        self.logged: list[tuple[str, str, dict]] = []

    async def log(self, run_id: str, action_type: str, config: dict, **kwargs: Any):
        self.logged.append((run_id, action_type, config))


def make_ctx(
    autonomy_level: int = 2,
    target_experiment_id: str | None = None,
    pool: Any = None,
) -> AgentContext:
    return AgentContext(
        pool=pool,
        vector_store=None,
        audit=_FakeAudit(),
        run_id="run-1",
        project_id="apdl",
        autonomy_level=autonomy_level,
        time_range_days=7,
        target_experiment_id=target_experiment_id,
    )


def _experiment(key: str = "exp_cta", days_ago: int = 10) -> dict[str, Any]:
    started = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {
        "key": key,
        "flag_key": key,
        "status": "running",
        "description": "Sticky CTA lifts signups",
        "primary_metric": {"event": "signup", "type": "conversion"},
        "variants": [{"key": "control", "weight": 50}, {"key": "treatment", "weight": 50}],
        "start_date": started,
        "created_at": started,
    }


def _results(control_users: int = 500, treatment_users: int = 500,
             significant: bool = True) -> dict[str, Any]:
    return {
        "variants": [
            {"variant": "control", "users": control_users, "mean": 0.10},
            {"variant": "treatment", "users": treatment_users, "mean": 0.13},
        ],
        "effect_size": 0.03,
        "p_value": 0.01 if significant else 0.4,
        "is_significant": significant,
        "recommendation": "",
    }


def _entry(exp: dict, results: dict) -> dict[str, Any]:
    agent = ExperimentEvaluationAgent()
    return {"experiment": exp, "results": results, "maturity": agent._maturity(exp, results)}


# ---------------------------------------------------------------------------
# maturity gate
# ---------------------------------------------------------------------------


def test_maturity_passes_with_sample_and_runtime():
    agent = ExperimentEvaluationAgent()
    m = agent._maturity(_experiment(days_ago=10), _results(significant=False))
    assert m["mature"] is True and m["reasons"] == []


def test_maturity_fails_underpowered_and_young():
    agent = ExperimentEvaluationAgent()
    m = agent._maturity(_experiment(days_ago=2), _results(50, 60, significant=False))
    assert m["mature"] is False
    assert any("users" in r for r in m["reasons"])
    assert any("days" in r for r in m["reasons"])


def test_maturity_early_stop_on_significance():
    agent = ExperimentEvaluationAgent()
    # Half the sample floor but already significant → sequential-style early stop.
    m = agent._maturity(_experiment(days_ago=2), _results(120, 130, significant=True))
    assert m["mature"] is True and m["significant_early_stop"] is True


def test_maturity_fails_without_exposure_data():
    agent = ExperimentEvaluationAgent()
    m = agent._maturity(_experiment(), {})
    assert m["mature"] is False
    assert "no exposure data yet" in m["reasons"]


# ---------------------------------------------------------------------------
# act(): verdict execution
# ---------------------------------------------------------------------------


def _verdict(experiment_id: str, verdict: str, durable: str = "") -> dict[str, Any]:
    return {
        "experiment_id": experiment_id,
        "verdict": verdict,
        "reasoning": "numbers say so",
        "key_numbers": {"p_value": 0.01},
        "durable_feature": durable,
    }


@pytest.mark.asyncio
async def test_ship_completes_experiment_and_records(monkeypatch):
    agent = ExperimentEvaluationAgent()
    status_calls: list[tuple[str, str]] = []
    recorded: list[dict[str, Any]] = []

    async def fake_status(project_id, experiment_id, status):
        status_calls.append((experiment_id, status))
        return {}

    async def fake_record(pool, project_id, run_id, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(experiment_evaluation, "update_experiment_status", fake_status)
    monkeypatch.setattr(experiment_evaluation, "record_verdict", fake_record)

    working = {"candidates": [_entry(_experiment(), _results())], "immature": []}
    meta = await agent.act(
        make_ctx(autonomy_level=2, pool=object()), {}, working,
        [_verdict("exp_cta", "ship", durable="Make the sticky CTA permanent.")],
    )

    assert status_calls == [("exp_cta", "completed")]
    assert meta["verdict_counts"] == {"ship": 1}
    assert recorded and recorded[0]["verdict"] == "ship"
    assert recorded[0]["durable_feature"] == "Make the sticky CTA permanent."


@pytest.mark.asyncio
async def test_rollback_stops_disables_flag_and_reverts_changeset(monkeypatch):
    agent = ExperimentEvaluationAgent()
    calls: list[str] = []

    async def fake_status(project_id, experiment_id, status):
        calls.append(f"status:{status}")
        return {}

    async def fake_disable(project_id, key, **kwargs):
        calls.append(f"disable:{key}")
        return {}

    async def fake_ledger(pool, project_id, experiment_id):
        return {"experiment_id": experiment_id, "changeset_id": "cs-7"}

    async def fake_revert(project_id, changeset_id):
        assert project_id == "apdl"
        calls.append(f"revert:{changeset_id}")
        return {}

    async def fake_record(pool, project_id, run_id, **kwargs):
        pass

    monkeypatch.setattr(experiment_evaluation, "update_experiment_status", fake_status)
    monkeypatch.setattr(experiment_evaluation, "disable_flag", fake_disable)
    monkeypatch.setattr(experiment_evaluation, "get_designed_experiment", fake_ledger)
    monkeypatch.setattr(experiment_evaluation, "revert_changeset", fake_revert)
    monkeypatch.setattr(experiment_evaluation, "record_verdict", fake_record)

    working = {"candidates": [_entry(_experiment(), _results())], "immature": []}
    await agent.act(
        make_ctx(autonomy_level=2, pool=object()), {}, working,
        [_verdict("exp_cta", "rollback")],
    )

    assert calls == ["status:stopped", "disable:exp_cta", "revert:cs-7"]


@pytest.mark.asyncio
async def test_iterate_stops_and_releases_insight(monkeypatch):
    agent = ExperimentEvaluationAgent()
    released: list[tuple[str, str]] = []

    async def fake_status(project_id, experiment_id, status):
        return {}

    async def fake_release(pool, project_id, experiment_id, status):
        released.append((experiment_id, status))

    async def fake_record(pool, project_id, run_id, **kwargs):
        pass

    monkeypatch.setattr(experiment_evaluation, "update_experiment_status", fake_status)
    monkeypatch.setattr(experiment_evaluation, "set_designed_experiment_status", fake_release)
    monkeypatch.setattr(experiment_evaluation, "record_verdict", fake_record)

    working = {"candidates": [_entry(_experiment(), _results(significant=False))], "immature": []}
    await agent.act(
        make_ctx(autonomy_level=2, pool=object()), {}, working,
        [_verdict("exp_cta", "iterate")],
    )
    assert released == [("exp_cta", "iterate_requested")]


@pytest.mark.asyncio
async def test_l1_records_verdict_but_touches_nothing(monkeypatch):
    agent = ExperimentEvaluationAgent()
    recorded: list[dict[str, Any]] = []

    async def fail_status(*args, **kwargs):
        raise AssertionError("L1 must not change experiment status")

    async def fake_record(pool, project_id, run_id, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(experiment_evaluation, "update_experiment_status", fail_status)
    monkeypatch.setattr(experiment_evaluation, "record_verdict", fake_record)

    working = {"candidates": [_entry(_experiment(), _results())], "immature": []}
    meta = await agent.act(
        make_ctx(autonomy_level=1, pool=object()), {}, working,
        [_verdict("exp_cta", "ship")],
    )
    assert recorded and recorded[0]["verdict"] == "ship"
    assert meta["verdicts"][0]["actions"]["applied"] is False


@pytest.mark.asyncio
async def test_hallucinated_experiment_id_is_dropped(monkeypatch):
    agent = ExperimentEvaluationAgent()

    async def fail_status(*args, **kwargs):
        raise AssertionError("must not act on an experiment the model was not given")

    monkeypatch.setattr(experiment_evaluation, "update_experiment_status", fail_status)

    working = {"candidates": [_entry(_experiment(), _results())], "immature": []}
    meta = await agent.act(
        make_ctx(autonomy_level=2), {}, working,
        [_verdict("exp_ghost", "rollback")],
    )
    assert meta["evaluated"] == 0


@pytest.mark.asyncio
async def test_scoped_run_reports_immature_verdict(monkeypatch):
    agent = ExperimentEvaluationAgent()
    recorded: list[dict[str, Any]] = []

    async def fake_record(pool, project_id, run_id, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(experiment_evaluation, "record_verdict", fake_record)

    young = _entry(_experiment(days_ago=1), _results(10, 12, significant=False))
    working = {"candidates": [], "immature": [young]}
    meta = await agent.act(
        make_ctx(autonomy_level=2, target_experiment_id="exp_cta", pool=object()),
        {}, working, [],
    )

    assert meta["verdict_counts"] == {"immature": 1}
    assert recorded and recorded[0]["verdict"] == "immature"
    assert "users" in recorded[0]["reasoning"]


@pytest.mark.asyncio
async def test_unscoped_run_skips_immature_silently():
    agent = ExperimentEvaluationAgent()
    young = _entry(_experiment(days_ago=1), _results(10, 12, significant=False))
    working = {"candidates": [], "immature": [young]}
    meta = await agent.act(make_ctx(autonomy_level=2), {}, working, [])
    assert meta["evaluated"] == 0


# ---------------------------------------------------------------------------
# gather: registry scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gather_scopes_to_target_and_errors_when_missing(monkeypatch):
    agent = ExperimentEvaluationAgent()

    async def fake_active(project_id):
        return [_experiment("exp_a"), _experiment("exp_b")]

    async def fake_results(**kwargs):
        return _results()

    monkeypatch.setattr(experiment_evaluation, "get_active_experiments", fake_active)
    monkeypatch.setattr(experiment_evaluation, "get_experiment_results", fake_results)

    state: dict[str, Any] = {}
    working = await agent.gather(make_ctx(target_experiment_id="exp_b"), state, {})
    keys = [c["experiment"]["key"] for c in working["candidates"]]
    assert keys == ["exp_b"]

    state = {}
    working = await agent.gather(make_ctx(target_experiment_id="exp_missing"), state, {})
    assert working["candidates"] == []
    assert state["errors"] and "exp_missing" in state["errors"][0]


@pytest.mark.asyncio
async def test_gather_only_considers_running_experiments(monkeypatch):
    agent = ExperimentEvaluationAgent()

    async def fake_active(project_id):
        stopped = _experiment("exp_done")
        stopped["status"] = "completed"
        return [stopped, _experiment("exp_live")]

    async def fake_results(**kwargs):
        return _results()

    monkeypatch.setattr(experiment_evaluation, "get_active_experiments", fake_active)
    monkeypatch.setattr(experiment_evaluation, "get_experiment_results", fake_results)

    working = await agent.gather(make_ctx(), {}, {})
    keys = [c["experiment"]["key"] for c in working["candidates"]]
    assert keys == ["exp_live"]
