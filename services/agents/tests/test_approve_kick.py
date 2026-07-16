"""Per-item approval: forks one run per approved proposal, deploys approved
designs, audits each decision, and always resumes (never wedges at resuming)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import approvals
from app.store.run_leases import recover_abandoned_runs

_PROPOSAL = {
    "proposal_id": "p1",
    "title": "Add dark mode",
    "proposed_solution": "Implement a dark-mode toggle across the app.",
}
_PROPOSAL2 = {
    "proposal_id": "p2",
    "title": "Add CSV export",
    "proposed_solution": "Export tables as CSV.",
}


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.executed: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        # Result of the atomic gate-claim UPDATE ... RETURNING run_id.
        # None simulates losing the claim race to a concurrent submit.
        self.claim_result: Any = "run-1"

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: Any):
        if "FROM agent_runs" in query:
            return self.store["run"]
        if "FROM feature_proposals" in query:
            # The post-enqueue claimability check: the enqueued proposal
            # exists as an approved row unless the test says otherwise.
            project_id, proposal_id = args
            return self.store.get(
                "proposal_rows", {}
            ).get(
                (project_id, proposal_id),
                {
                    "project_id": project_id,
                    "proposal_id": proposal_id,
                    "status": "approved",
                },
            )
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any):
        if "FROM agent_run_results" in query:
            return self.store["results"]
        if "SET status = 'failed'" in query:
            run = self.store["run"]
            if run.get("lease_expires_at") != "expired":
                return []
            run.update(
                status="failed",
                phase="orphaned",
                lease_owner_id=None,
                lease_expires_at=None,
            )
            return [{"run_id": run["run_id"]}]
        if "claim_run_id = ANY" in query or "proposal.claim_run_id IS NULL" in query:
            return []
        raise AssertionError(f"Unexpected fetch: {query}")

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any):
        self.fetchvals.append((query, args))
        if "agent_audit_log" in query:
            return 1
        run = self.store["run"]
        if "status = 'waiting_approval'" in query:
            if (
                self.claim_result is None
                or run.get("lease_owner_id") is not None
                or run.get("lease_expires_at") is not None
            ):
                return None
            run.update(
                status=args[1],
                phase="resuming",
                lease_owner_id=args[4],
                lease_expires_at="live",
            )
            return run["run_id"]
        if "phase = 'resuming'" in query and "SET lease_owner_id = NULL" in query:
            if run.get("lease_owner_id") != args[1]:
                return None
            run.update(lease_owner_id=None, lease_expires_at="queued")
            self.store["handoff_count"] = self.store.get("handoff_count", 0) + 1
            return run["run_id"]
        if "SET lease_expires_at" in query:
            if run.get("lease_owner_id") != args[1]:
                return None
            run["lease_expires_at"] = "live"
            return run["run_id"]
        raise AssertionError(f"Unexpected fetchval: {query}")


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


def _run_row(
    status: str = "waiting_approval",
    level: int = 3,
    phase: str = "feature_proposal_approval",
    analysis_types: tuple[str, ...] = ("feature_proposal",),
) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "status": status,
        "phase": phase,
        "project_id": "demo",
        "autonomy_level": level,
        "config": json.dumps({"analysis_types": list(analysis_types), "time_range_days": 7}),
        "lease_owner_id": None,
        "lease_expires_at": None,
    }


def _patch(monkeypatch):
    enq: list = []
    kicked: list = []
    deployed: list = []

    async def fake_enqueue(pool, run_id, project_id, proposals):
        enq.append((run_id, project_id, proposals))
        return len(proposals)

    async def fake_supervisor(**kwargs):
        kicked.append(kwargs)

    async def fake_deploy(project_id, experiment):
        deployed.append((project_id, experiment))
        return True

    async def fake_treatment(pool, project_id, run_id, design):
        return ""

    monkeypatch.setattr(approvals, "enqueue_proposals", fake_enqueue)
    monkeypatch.setattr(approvals, "run_supervisor", fake_supervisor)
    monkeypatch.setattr(approvals, "deploy_experiment", fake_deploy)
    monkeypatch.setattr(approvals, "open_treatment_changeset", fake_treatment)
    return enq, kicked, deployed


class _FakeVectorStore:
    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

    async def store(self, project_id: str, content: str, metadata: dict | None = None):
        self.stored.append({"project_id": project_id, "content": content, "metadata": metadata})
        return len(self.stored)


def _client(store: dict[str, Any]) -> AsyncClient:
    app.state.pg_pool = _FakePool(store)
    app.state.vector_store = _FakeVectorStore()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _forks(kicked: list) -> list:
    return [k for k in kicked if k.get("analysis_types") == ["code_implementation"]]


def _resumes(kicked: list) -> list:
    return [k for k in kicked if k.get("resume")]


@pytest.mark.asyncio
async def test_approval_requires_agents_approve_role():
    async def authenticate_runner(request: Request):
        principal = Principal(
            credential_id="runner",
            project_id="demo",
            roles=frozenset({"agents:run"}),
            self_registered_project=False,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_runner
    store = {"run": _run_row(), "results": []}
    async with _client(store) as client:
        response = await client.post(
            "/v1/agents/run-1/approve",
            json={"approved": True},
        )

    assert response.status_code == 403
    assert app.state.pg_pool.conn.executed == []


@pytest.mark.asyncio
async def test_self_registered_overprivileged_project_cannot_apply_approval(
    monkeypatch,
):
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

    app.dependency_overrides[authenticate_request] = authenticate_self_registered
    enqueued, supervisor_calls, deployed = _patch(monkeypatch)
    store = {
        "run": _run_row(),
        "results": [{"output": json.dumps([_PROPOSAL])}],
    }

    async with _client(store) as client:
        response = await client.post(
            "/v1/agents/run-1/approve",
            json={"approved": True},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Agents execution is unavailable for self-registered projects"
    )
    assert store["run"]["status"] == "waiting_approval"
    assert app.state.pg_pool.conn.fetchvals == []
    assert app.state.pg_pool.conn.executed == []
    assert enqueued == []
    assert deployed == []
    assert supervisor_calls == []


@pytest.mark.asyncio
async def test_legacy_feature_proposal_approval_is_quarantined(monkeypatch):
    enq, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_count"] == 1 and body["rejected_count"] == 0

    assert _forks(kicked) == []
    assert body["forked_runs"] == []
    assert body["errors"] == ["feature proposal unavailable: p1"]
    assert enq == []

    # Bug B: the run always resumes (so it finalizes instead of wedging at resuming).
    assert _resumes(kicked) and _resumes(kicked)[0]["run_id"] == "run-1"

    # The decision is audited per item with a JSON-string config (never a raw dict).
    human = [
        a for q, a in app.state.pg_pool.conn.fetchvals
        if "agent_audit_log" in q and a[1] == "human_approval"
    ]
    assert human and isinstance(human[0][2], str)
    assert json.loads(human[0][2])["item_id"] == "p1"


@pytest.mark.asyncio
async def test_per_item_mixed_forks_only_approved(monkeypatch):
    enq, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL, _PROPOSAL2])}]}

    async with _client(store) as client:
        resp = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "p1", "approved": True}, {"item_id": "p2", "approved": False}]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 1 and body["rejected_count"] == 1
    forks = _forks(kicked)
    assert forks == []
    assert body["errors"] == ["feature proposal unavailable: p1"]
    assert enq == []


@pytest.mark.asyncio
async def test_per_item_approve_all_quarantines_each_legacy_proposal(monkeypatch):
    _, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL, _PROPOSAL2])}]}

    async with _client(store) as client:
        resp = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "p1", "approved": True}, {"item_id": "p2", "approved": True}]},
        )

    assert resp.status_code == 200
    forks = _forks(kicked)
    assert forks == []
    assert resp.json()["errors"] == [
        "feature proposal unavailable: p1",
        "feature proposal unavailable: p2",
    ]


@pytest.mark.asyncio
async def test_reject_all_resumes_without_forking(monkeypatch):
    enq, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": False})

    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert enq == [] and _forks(kicked) == []
    assert _resumes(kicked)  # rejecting an item != aborting the run


@pytest.mark.asyncio
async def test_unknown_item_id_is_422(monkeypatch):
    _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        resp = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "nope", "approved": True}]},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_missing_decision_is_422(monkeypatch):
    _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL, _PROPOSAL2])}]}

    async with _client(store) as client:
        resp = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "p1", "approved": True}]},  # p2 omitted
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_or_ambiguous_request_is_422(monkeypatch):
    _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        empty = await client.post("/v1/agents/run-1/approve", json={"decisions": []})
        neither = await client.post("/v1/agents/run-1/approve", json={})
        both = await client.post(
            "/v1/agents/run-1/approve",
            json={"approved": True, "decisions": [{"item_id": "p1", "approved": True}]},
        )

    assert empty.status_code == 422 and neither.status_code == 422 and both.status_code == 422


@pytest.mark.asyncio
async def test_experiment_design_deploys_approved_and_resumes(monkeypatch):
    enq, kicked, deployed = _patch(monkeypatch)
    design = {"experiment_id": "exp_demo", "flag_config": {"key": "exp_demo"}, "variants": []}
    store = {
        "run": _run_row(
            phase="experiment_design_approval",
            analysis_types=("experiment_design", "feature_proposal"),
        ),
        "results": [{"output": json.dumps([design])}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert deployed and deployed[0][0] == "demo" and deployed[0][1]["experiment_id"] == "exp_demo"
    # The SAME run resumes to continue the pipeline (feature_proposal); no code-impl fork here.
    resumes = _resumes(kicked)
    assert resumes and resumes[0]["run_id"] == "run-1"
    assert resumes[0]["analysis_types"] == ["experiment_design", "feature_proposal"]
    assert _forks(kicked) == [] and enq == []


@pytest.mark.asyncio
async def test_experiment_deploy_failure_is_returned_and_persisted_for_resume(monkeypatch):
    _, kicked, _ = _patch(monkeypatch)

    async def failed_deploy(project_id, experiment):
        return False

    monkeypatch.setattr(approvals, "deploy_experiment", failed_deploy)
    design = {
        "experiment_id": "exp_failed",
        "flag_config": {"key": "exp_failed"},
        "variants": [],
    }
    store = {
        "run": _run_row(
            phase="experiment_design_approval",
            analysis_types=("experiment_design", "feature_proposal"),
        ),
        "results": [{"output": json.dumps([design])}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert resp.json()["errors"] == ["experiment deploy failed: exp_failed"]
    result_query, result_args = next(
        (query, args)
        for query, args in app.state.pg_pool.conn.executed
        if "INSERT INTO agent_run_results" in query and "approval_errors" in query
    )
    assert "lease_owner_id = $3" in result_query
    assert json.loads(result_args[1]) == ["experiment deploy failed: exp_failed"]
    assert _resumes(kicked)


@pytest.mark.asyncio
async def test_experiment_design_reject_skips_deploy_but_resumes(monkeypatch):
    _, kicked, deployed = _patch(monkeypatch)
    design = {"experiment_id": "exp_demo", "flag_config": {"key": "exp_demo"}, "variants": []}
    store = {
        "run": _run_row(
            phase="experiment_design_approval",
            analysis_types=("experiment_design", "feature_proposal"),
        ),
        "results": [{"output": json.dumps([design])}],
    }

    async with _client(store) as client:
        resp = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "exp_demo", "approved": False}]},
        )

    assert resp.status_code == 200
    assert deployed == []  # rejected design not deployed
    assert _resumes(kicked)  # pipeline still continues to feature_proposal


@pytest.mark.asyncio
async def test_code_implementation_gate_does_not_refork(monkeypatch):
    enq, kicked, _ = _patch(monkeypatch)
    store = {
        "run": _run_row(
            phase="code_implementation_approval",
            analysis_types=("feature_proposal", "code_implementation"),
        ),
        "results": [],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert _forks(kicked) == []  # approving a changeset gate must not mint a new code-impl run
    assert enq == []
    assert _resumes(kicked)


_CHANGESET = {
    "proposal_id": "p1",
    "title": "Add dark mode",
    "spec": "Implement a dark-mode toggle across the app.",
    "decision": "approve",
}


@pytest.mark.asyncio
async def test_code_implementation_gate_opens_pr_on_approval(monkeypatch):
    """Phase 6: approving the changeset gate opens the PR (it was gated before
    opening) and marks the proposal implemented — without forking a new run."""
    _, kicked, _ = _patch(monkeypatch)
    opened: list = []
    implemented: list = []

    async def fake_open(**kwargs):
        opened.append(kwargs)
        return {"changeset_id": "cs_1", "status": "queued"}

    async def fake_implemented(pool, project_id, proposal_id, changeset_id, run_id):
        implemented.append((project_id, proposal_id, changeset_id, run_id))

    monkeypatch.setattr(approvals, "open_changeset", fake_open)
    monkeypatch.setattr(approvals, "mark_implemented", fake_implemented)

    store = {
        "run": _run_row(phase="code_implementation_approval", analysis_types=("code_implementation",)),
        "results": [{"output": json.dumps([_CHANGESET])}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["approved_count"] == 1 and body["opened_changesets"] == ["cs_1"]
    assert opened and opened[0]["title"] == "Add dark mode"
    assert opened[0]["project_id"] == "demo" and opened[0]["run_id"] == "run-1"
    assert opened[0]["context"] == {"proposal_id": "p1"}
    assert implemented == [("demo", "p1", "cs_1", "run-1")]
    # Opening a PR is not a new run fork — no kicked run carries a target proposal.
    assert not any(k.get("target_proposal_id") for k in kicked)
    assert _resumes(kicked)  # the run still resumes to finalize


@pytest.mark.asyncio
async def test_code_implementation_gate_reject_marks_failed(monkeypatch):
    """Rejecting the gate opens nothing and unsticks the proposal from 'implementing'."""
    _, kicked, _ = _patch(monkeypatch)
    opened: list = []
    failed: list = []

    async def fake_open(**kwargs):
        opened.append(kwargs)
        return {"changeset_id": "cs_1"}

    async def fake_failed(pool, project_id, proposal_id, error, run_id):
        failed.append((project_id, proposal_id, error, run_id))

    monkeypatch.setattr(approvals, "open_changeset", fake_open)
    monkeypatch.setattr(approvals, "mark_failed", fake_failed)

    store = {
        "run": _run_row(phase="code_implementation_approval", analysis_types=("code_implementation",)),
        "results": [{"output": json.dumps([_CHANGESET])}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": False})

    assert resp.status_code == 200
    assert opened == []  # nothing opened on reject
    assert failed == [
        ("demo", "p1", "PR rejected at the approval gate.", "run-1")
    ]
    assert resp.json()["opened_changesets"] == []
    assert _resumes(kicked)


@pytest.mark.asyncio
async def test_blanket_approve_skips_safety_halted_changeset(monkeypatch):
    """A multi-proposal drain can put a safety-halted changeset (decision='halt',
    already failed) in the same gate as an approvable one. A blanket approve must
    open ONLY the approvable item and skip the halted one — never open a PR for a
    changeset that failed safety nor overwrite its failed status."""
    _, kicked, _ = _patch(monkeypatch)
    opened: list = []
    implemented: list = []

    async def fake_open(**kwargs):
        opened.append(kwargs)
        return {"changeset_id": "cs_ok", "status": "queued"}

    async def fake_implemented(pool, project_id, proposal_id, changeset_id, run_id):
        implemented.append((project_id, proposal_id, changeset_id, run_id))

    monkeypatch.setattr(approvals, "open_changeset", fake_open)
    monkeypatch.setattr(approvals, "mark_implemented", fake_implemented)

    halted = {
        "proposal_id": "p2",
        "title": "Risky change",
        "spec": "Touches something safety flagged.",
        "decision": "halt",
        "safety_result": {"passed": False},
    }
    store = {
        "run": _run_row(phase="code_implementation_approval", analysis_types=("code_implementation",)),
        "results": [{"output": json.dumps([_CHANGESET, halted])}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    # Only the approvable changeset (p1) is opened; the halted p2 is skipped.
    assert [o["title"] for o in opened] == ["Add dark mode"]
    assert implemented == [("demo", "p1", "cs_ok", "run-1")]
    assert resp.json()["opened_changesets"] == ["cs_ok"]
    assert _resumes(kicked)


@pytest.mark.asyncio
async def test_not_waiting_approval_is_400(monkeypatch):
    _patch(monkeypatch)
    store = {"run": _run_row(status="running"), "results": []}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_lost_claim_race_is_400(monkeypatch):
    _, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        app.state.pg_pool.conn.claim_result = None  # another request won the gate first
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 400
    assert _forks(kicked) == []  # nothing forked when the claim is lost


@pytest.mark.asyncio
async def test_approval_does_not_overwrite_a_lingering_owner(monkeypatch):
    _, kicked, _ = _patch(monkeypatch)
    run = _run_row()
    run.update(lease_owner_id="finishing-worker", lease_expires_at="live")
    store = {"run": run, "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        response = await client.post(
            "/v1/agents/run-1/approve", json={"approved": True}
        )

    assert response.status_code == 400
    assert store["run"]["lease_owner_id"] == "finishing-worker"
    assert kicked == []


@pytest.mark.asyncio
async def test_experiment_gate_blanket_approve_skips_non_deployable_designs(monkeypatch):
    """A multi-design gate can hold a safety-halted or already-deployed sibling
    next to the design genuinely awaiting a human; a blanket approve must
    deploy only the approvable one and audit the skips."""
    _, kicked, deployed = _patch(monkeypatch)
    designs = [
        {"experiment_id": "exp_ok", "decision": "approve",
         "safety_result": {"passed": True}},
        {"experiment_id": "exp_halted", "decision": "halt",
         "safety_result": {"passed": False}},
        {"experiment_id": "exp_live", "decision": "deploy", "deployed": True,
         "safety_result": {"passed": True}},
    ]
    store = {
        "run": _run_row(phase="experiment_design_approval",
                        analysis_types=("experiment_design",)),
        "results": [{"output": json.dumps(designs)}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert [e[1]["experiment_id"] for e in deployed] == ["exp_ok"]
    skipped = [
        a for q, a in app.state.pg_pool.conn.fetchvals
        if "agent_audit_log" in q and a[1] == "approval_skipped"
    ]
    assert {json.loads(s[2])["item_id"] for s in skipped} == {"exp_halted", "exp_live"}
    assert _resumes(kicked)


@pytest.mark.asyncio
async def test_approved_experiment_opens_treatment_changeset(monkeypatch):
    """Phase 2: a human-approved experiment gets its treatment built — the
    approval deploys the experiment AND opens the codegen changeset."""
    _, kicked, deployed = _patch(monkeypatch)
    treatments: list = []

    async def fake_treatment(pool, project_id, run_id, design):
        treatments.append(design.get("experiment_id"))
        return "cs-treat-1"

    monkeypatch.setattr(approvals, "open_treatment_changeset", fake_treatment)
    design = {
        "experiment_id": "exp_demo",
        "flag_config": {"key": "exp_demo"},
        "variants": [],
        "treatment_spec": "Add the sticky CTA.",
    }
    store = {
        "run": _run_row(phase="experiment_design_approval",
                        analysis_types=("experiment_design",)),
        "results": [{"output": json.dumps([design])}],
    }

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    assert deployed and treatments == ["exp_demo"]
    assert resp.json()["opened_changesets"] == ["cs-treat-1"]
    assert _resumes(kicked)


@pytest.mark.asyncio
async def test_slow_approval_keeps_owner_heartbeat_against_competing_reaper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, kicked, _ = _patch(monkeypatch)
    effect_started = asyncio.Event()
    finish_effect = asyncio.Event()
    heartbeat_started = asyncio.Event()
    real_maintain = approvals.maintain_run_lease

    async def tracked_maintain(*args: Any, **kwargs: Any) -> None:
        heartbeat_started.set()
        await real_maintain(*args, **kwargs)

    async def slow_deploy(project_id: str, design: dict[str, Any]) -> bool:
        effect_started.set()
        await finish_effect.wait()
        return True

    monkeypatch.setattr(approvals, "maintain_run_lease", tracked_maintain)
    monkeypatch.setattr(approvals, "deploy_experiment", slow_deploy)
    design = {"experiment_id": "exp_slow", "flag_config": {"key": "exp_slow"}}
    store = {
        "run": _run_row(
            phase="experiment_design_approval",
            analysis_types=("experiment_design",),
        ),
        "results": [{"output": json.dumps([design])}],
    }

    async with _client(store) as client:
        request_task = asyncio.create_task(
            client.post("/v1/agents/run-1/approve", json={"approved": True})
        )
        await asyncio.wait_for(effect_started.wait(), timeout=1)

        run = app.state.pg_pool.conn.store["run"]
        assert heartbeat_started.is_set()
        assert run["phase"] == "resuming"
        assert run["lease_owner_id"] is not None
        assert run["lease_expires_at"] == "live"

        competing = await recover_abandoned_runs(app.state.pg_pool)
        assert competing.abandoned_run_ids == ()
        assert run["status"] == "approved"

        finish_effect.set()
        response = await asyncio.wait_for(request_task, timeout=1)

    assert response.status_code == 200
    assert store["handoff_count"] == 1
    assert store["run"]["lease_owner_id"] is None
    assert store["run"]["lease_expires_at"] == "queued"
    assert _resumes(kicked)


@pytest.mark.asyncio
async def test_approval_lease_loss_cancels_config_before_codegen_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    config_started = asyncio.Event()
    config_cancelled = asyncio.Event()
    effects: list[str] = []

    async def lose_during_config(
        pool: Any,
        run_id: str,
        owner_id: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        await config_started.wait()
        lost.set()
        await stop.wait()

    async def blocked_config(project_id: str, design: dict[str, Any]) -> bool:
        effects.append("config-started")
        config_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            effects.append("config-cancelled")
            config_cancelled.set()
            raise
        return True

    async def forbidden_codegen(*args: Any, **kwargs: Any) -> str:
        effects.append("codegen-after-loss")
        return "cs-should-not-open"

    monkeypatch.setattr(approvals, "maintain_run_lease", lose_during_config)
    monkeypatch.setattr(approvals, "deploy_experiment", blocked_config)
    monkeypatch.setattr(approvals, "open_treatment_changeset", forbidden_codegen)
    design = {"experiment_id": "exp_loss", "flag_config": {"key": "exp_loss"}}
    store = {
        "run": _run_row(
            phase="experiment_design_approval",
            analysis_types=("experiment_design",),
        ),
        "results": [{"output": json.dumps([design])}],
    }

    async with _client(store) as client:
        response = await client.post(
            "/v1/agents/run-1/approve", json={"approved": True}
        )

    assert response.status_code == 503
    assert config_cancelled.is_set()
    assert effects == ["config-started", "config-cancelled"]
    assert store.get("handoff_count", 0) == 0
    assert store["run"]["lease_owner_id"] is not None
    assert store["run"]["lease_expires_at"] == "live"


@pytest.mark.asyncio
async def test_queued_resume_and_forks_start_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def blocking_supervisor(**kwargs: Any) -> None:
        started.add(str(kwargs["run_id"]))
        if len(started) == 2:
            both_started.set()
        await release.wait()

    monkeypatch.setattr(approvals, "run_supervisor", blocking_supervisor)
    batch = asyncio.create_task(
        approvals._run_supervisor_batch(
            [{"run_id": "run-resume"}, {"run_id": "run-fork"}]
        )
    )

    await asyncio.wait_for(both_started.wait(), timeout=1)
    assert started == {"run-resume", "run-fork"}
    release.set()
    await asyncio.wait_for(batch, timeout=1)
