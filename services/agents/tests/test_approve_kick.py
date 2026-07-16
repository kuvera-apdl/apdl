"""Per-item approval: forks one run per approved proposal, deploys approved
designs, audits each decision, and always resumes (never wedges at resuming)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import approvals

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


class _FakeConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store
        self.executed: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        # Result of the atomic gate-claim UPDATE ... RETURNING run_id.
        # None simulates losing the claim race to a concurrent submit.
        self.claim_result: Any = "run-1"

    async def fetchrow(self, query: str, *args: Any):
        if "FROM agent_runs" in query:
            return self.store["run"]
        if "FROM feature_proposals" in query:
            # The post-enqueue claimability check: the enqueued proposal
            # exists as an approved row unless the test says otherwise.
            proposal_id = args[0]
            return self.store.get(
                "proposal_rows", {}
            ).get(proposal_id, {"proposal_id": proposal_id, "status": "approved"})
        raise AssertionError(f"Unexpected fetchrow: {query}")

    async def fetch(self, query: str, *args: Any):
        if "FROM agent_run_results" in query:
            return self.store["results"]
        raise AssertionError(f"Unexpected fetch: {query}")

    async def execute(self, query: str, *args: Any):
        self.executed.append((query, args))

    async def fetchval(self, query: str, *args: Any):
        self.fetchvals.append((query, args))
        if "agent_audit_log" in query:
            return 1
        return self.claim_result  # the gate-claim UPDATE


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
async def test_legacy_approved_forks_single_proposal(monkeypatch):
    enq, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL])}]}

    async with _client(store) as client:
        resp = await client.post("/v1/agents/run-1/approve", json={"approved": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["approved_count"] == 1 and body["rejected_count"] == 0

    forks = _forks(kicked)
    assert len(forks) == 1
    assert forks[0]["project_id"] == "demo" and forks[0]["autonomy_level"] == 3
    assert forks[0]["target_proposal_id"] == "p1"
    assert body["forked_runs"] == [forks[0]["run_id"]]
    assert enq and enq[0][2][0]["proposal_id"] == "p1"

    # The forked run's config must be a JSON *string* for the jsonb column;
    # a raw dict makes asyncpg raise "expected str, got dict".
    _, run_insert_args = next(
        (q, a) for q, a in app.state.pg_pool.conn.executed if "INSERT INTO agent_runs" in q
    )
    assert isinstance(run_insert_args[-1], str)
    cfg = json.loads(run_insert_args[-1])
    assert cfg["analysis_types"] == ["code_implementation"] and cfg["target_proposal_id"] == "p1"

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
    assert len(forks) == 1 and forks[0]["target_proposal_id"] == "p1"
    assert [e[2][0]["proposal_id"] for e in enq] == ["p1"]  # only the approved one enqueued


@pytest.mark.asyncio
async def test_per_item_approve_all_forks_each_distinctly(monkeypatch):
    _, kicked, _ = _patch(monkeypatch)
    store = {"run": _run_row(), "results": [{"output": json.dumps([_PROPOSAL, _PROPOSAL2])}]}

    async with _client(store) as client:
        resp = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "p1", "approved": True}, {"item_id": "p2", "approved": True}]},
        )

    assert resp.status_code == 200
    forks = _forks(kicked)
    assert sorted(f["target_proposal_id"] for f in forks) == ["p1", "p2"]
    assert len({f["run_id"] for f in forks}) == 2  # one distinct run per proposal


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

    async def fake_implemented(pool, proposal_id, changeset_id):
        implemented.append((proposal_id, changeset_id))

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
    assert implemented == [("p1", "cs_1")]
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

    async def fake_failed(pool, proposal_id, error):
        failed.append((proposal_id, error))

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
    assert failed == [("p1", "PR rejected at the approval gate.")]
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

    async def fake_implemented(pool, proposal_id, changeset_id):
        implemented.append((proposal_id, changeset_id))

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
    assert implemented == [("p1", "cs_ok")]
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
