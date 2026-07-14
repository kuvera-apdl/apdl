"""Phase 4: feature_proposal drains the ship-verdict queue — no win, no proposal."""

from __future__ import annotations

from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs import feature_proposal as fp


def _ctx(pool: Any = None) -> AgentContext:
    return AgentContext(
        pool=pool,
        vector_store=None,
        audit=None,
        run_id="run-1",
        project_id="demo",
        autonomy_level=3,
        time_range_days=7,
    )


def _verdict(verdict_id: int, experiment_id: str) -> dict[str, Any]:
    return {
        "id": verdict_id,
        "experiment_id": experiment_id,
        "verdict": "ship",
        "reasoning": "significant win",
        "results": {"key_numbers": {"p_value": 0.01}},
        "durable_feature": f"Make {experiment_id}'s treatment permanent.",
        "consumed": False,
    }


@pytest.mark.asyncio
async def test_gather_stops_at_verdict_queue_when_empty(monkeypatch):
    """With no wins, gather must not even fetch repo context — the agent is done."""

    async def fake_list(pool, project_id, limit):
        return []

    async def fail_repo(project_id):
        raise AssertionError("repo context must not be fetched when there are no wins")

    monkeypatch.setattr(fp, "list_unconsumed_ship_verdicts", fake_list)
    monkeypatch.setattr(fp, "get_repo_context", fail_repo)

    out = await fp.FeatureProposalAgent().gather(_ctx(pool=object()), {}, {})
    assert out == {"ship_verdicts": []}


@pytest.mark.asyncio
async def test_gather_grounds_wins_with_repo_and_existing_work(monkeypatch):
    async def fake_list(pool, project_id, limit):
        return [_verdict(1, "exp_cta")]

    async def fake_repo(project_id):
        return {"repo": "acme/widgets", "framework": "Next.js"}

    async def fake_recent(pool, project_id):
        return [{"title": "Bot filter", "status": "implemented"}]

    async def fake_changesets(project_id):
        return []

    monkeypatch.setattr(fp, "list_unconsumed_ship_verdicts", fake_list)
    monkeypatch.setattr(fp, "get_repo_context", fake_repo)
    monkeypatch.setattr(fp, "list_recent_proposals", fake_recent)
    monkeypatch.setattr(fp, "list_changesets", fake_changesets)

    out = await fp.FeatureProposalAgent().gather(_ctx(pool=object()), {}, {})
    assert [v["experiment_id"] for v in out["ship_verdicts"]] == ["exp_cta"]
    assert out["repo_context"]["repo"] == "acme/widgets"
    assert out["recent_proposals"][0]["title"] == "Bot filter"


@pytest.mark.asyncio
async def test_act_marks_drained_verdicts_consumed(monkeypatch):
    consumed: list[list[int]] = []

    async def fake_mark(pool, verdict_ids):
        consumed.append(verdict_ids)

    monkeypatch.setattr(fp, "mark_verdicts_consumed", fake_mark)

    working = {"ship_verdicts": [_verdict(7, "exp_a"), _verdict(9, "exp_b")]}
    proposals = [{"proposal_id": "feat_a", "source_experiment_id": "exp_a"}]
    meta = await fp.FeatureProposalAgent().act(_ctx(pool=object()), {}, working, proposals)

    assert consumed == [[7, 9]]
    assert meta["needs_approval"] is True and meta["proposals_count"] == 1


@pytest.mark.asyncio
async def test_act_without_proposals_still_gates_nothing(monkeypatch):
    async def fake_mark(pool, verdict_ids):
        pass

    monkeypatch.setattr(fp, "mark_verdicts_consumed", fake_mark)
    meta = await fp.FeatureProposalAgent().act(_ctx(pool=object()), {}, {"ship_verdicts": []}, [])
    assert meta["needs_approval"] is False and meta["approval_status"] == "none"


def test_prompt_demands_flag_removal_semantics():
    # The system prompt is the contract with codegen — pin its core clauses.
    assert "flag branch" in fp.FEATURE_PROPOSAL_SYSTEM
    assert "No win, no proposal" in fp.FEATURE_PROPOSAL_SYSTEM
    assert "source_experiment_id" in fp.FEATURE_PROPOSAL_SYSTEM
