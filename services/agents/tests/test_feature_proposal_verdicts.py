"""Legacy significance-derived proposal rows remain quarantined."""

from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs.feature_proposal import FeatureProposalAgent


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


@pytest.mark.asyncio
async def test_feature_proposal_agent_never_reads_legacy_ship_verdicts():
    agent = FeatureProposalAgent()

    assert agent.enabled is False
    assert await agent.gather(_ctx(pool=object()), {}, {}) == {
        "disabled": True,
        "ship_verdicts": [],
    }
    assert agent.build_prompt(_ctx(), {}, {"ship_verdicts": [{"verdict": "ship"}]}) is None


@pytest.mark.asyncio
async def test_feature_proposal_agent_never_emits_or_consumes_proposals():
    result = await FeatureProposalAgent().act(
        _ctx(pool=object()),
        {},
        {"ship_verdicts": [{"id": 7, "verdict": "ship"}]},
        [{"proposal_id": "legacy-false-winner"}],
    )

    assert result == {
        "disabled": True,
        "proposals_count": 0,
        "approval_status": "unavailable",
        "needs_approval": False,
    }
