"""Tests for the code_implementation agent (queue + codegen client mocked)."""

import pytest

from app.framework import AgentContext
from app.graphs import code_implementation
from app.graphs.code_implementation import CodeImplementationAgent

_PROPOSAL = {
    "proposal_id": "p1",
    "title": "Add dark mode",
    "spec": "Implement a dark-mode toggle across the app.",
}


def _make_ctx(level: int = 3) -> AgentContext:
    return AgentContext(
        pool=None,
        vector_store=None,
        audit=None,
        run_id="run-1",
        project_id="apdl",
        autonomy_level=level,
    )


def _patch(monkeypatch, proposals, *, open_result=None):
    calls: dict[str, list] = {"opened": [], "implemented": [], "failed": []}

    async def fake_claim(pool, project_id, limit=5):
        return list(proposals)

    async def fake_open(**kwargs):
        calls["opened"].append(kwargs)
        return open_result or {"changeset_id": "cs_1", "status": "queued"}

    async def fake_implemented(pool, proposal_id, changeset_id):
        calls["implemented"].append((proposal_id, changeset_id))

    async def fake_failed(pool, proposal_id, error):
        calls["failed"].append((proposal_id, error))

    monkeypatch.setattr(code_implementation, "claim_proposals", fake_claim)
    monkeypatch.setattr(code_implementation, "open_changeset", fake_open)
    monkeypatch.setattr(code_implementation, "mark_implemented", fake_implemented)
    monkeypatch.setattr(code_implementation, "mark_failed", fake_failed)
    return calls


@pytest.mark.asyncio
async def test_l3_opens_changeset_and_marks_implemented(monkeypatch):
    calls = _patch(monkeypatch, [_PROPOSAL])

    result = await CodeImplementationAgent().run(_make_ctx(3), {})

    assert len(result.output) == 1
    assert result.output[0]["decision"] == "deploy"
    assert result.output[0]["changeset_id"] == "cs_1"
    assert calls["opened"][0]["project_id"] == "apdl"
    assert calls["opened"][0]["title"] == "Add dark mode"
    assert calls["opened"][0]["run_id"] == "run-1"
    assert calls["implemented"] == [("p1", "cs_1")]
    assert result.metadata["opened"] == 1


@pytest.mark.asyncio
async def test_l2_routes_to_approval_without_opening(monkeypatch):
    calls = _patch(monkeypatch, [_PROPOSAL])

    result = await CodeImplementationAgent().run(_make_ctx(2), {})

    assert result.output[0]["decision"] == "approve"
    assert result.metadata["needs_approval"] is True
    assert calls["opened"] == []
    assert calls["implemented"] == []


@pytest.mark.asyncio
async def test_l1_is_suggest_only_and_claims_nothing(monkeypatch):
    calls = _patch(monkeypatch, [_PROPOSAL])

    result = await CodeImplementationAgent().run(_make_ctx(1), {})

    assert result.output == []
    assert calls["opened"] == []


@pytest.mark.asyncio
async def test_bad_spec_fails_safety_and_marks_failed(monkeypatch):
    calls = _patch(
        monkeypatch, [{"proposal_id": "p2", "title": "x", "spec": "short"}]
    )

    result = await CodeImplementationAgent().run(_make_ctx(3), {})

    assert result.output[0]["decision"] == "halt"
    assert result.output[0]["safety_result"]["passed"] is False
    assert calls["opened"] == []
    assert calls["failed"][0][0] == "p2"


@pytest.mark.asyncio
async def test_codegen_failure_marks_proposal_failed(monkeypatch):
    calls = _patch(monkeypatch, [_PROPOSAL])

    async def boom(**kwargs):
        raise RuntimeError("codegen down")

    monkeypatch.setattr(code_implementation, "open_changeset", boom)

    result = await CodeImplementationAgent().run(_make_ctx(4), {})

    assert "error" in result.output[0]
    assert calls["failed"][0] == ("p1", "codegen down")
