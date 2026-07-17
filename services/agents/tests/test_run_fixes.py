"""Regression tests for the agent-run failures found in the admin console.

Covers: feature_proposal None-safety and parse_llm_json never raising on
malformed safety-review responses. (The behavior agent's plan-executor
regressions — event_names vs selectors, catalog formatting — died with the
plan-executor itself: the agent now drives the tool catalog agentically and
every call is validated by the catalog's pydantic params models; see
test_behavior_analysis_agent.py.)
"""

from __future__ import annotations

from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs import feature_proposal as fp
from app.llm.utils import parse_llm_json


def _ctx(**ov: Any) -> AgentContext:
    return AgentContext(
        pool=None,
        vector_store=None,
        audit=None,
        run_id="run-1",
        project_id="demo",
        autonomy_level=ov.get("autonomy_level", 3),
        time_range_days=ov.get("time_range_days", 7),
    )


# ---------------------------------------------------------------------------
# feature_proposal without a DB pool (tests, degraded runs) must degrade to
# "no wins" instead of crashing — and then skip the LLM entirely.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feature_proposal_gather_degrades_without_pool():
    agent = fp.FeatureProposalAgent()
    out = await agent.gather(_ctx(), {}, {})
    assert out == {"disabled": True, "ship_verdicts": []}
    assert agent.build_prompt(_ctx(), {}, out) is None


# ---------------------------------------------------------------------------
# parse_llm_json: tolerate fences / malformed input, never raise
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    assert parse_llm_json('{"approved": true}') == {"approved": True}


def test_parse_fenced_json():
    assert parse_llm_json('reasoning...\n```json\n{"approved": false}\n```') == {
        "approved": False
    }


def test_parse_generic_fence():
    assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}


def test_parse_malformed_fence_returns_fallback_without_raising():
    assert parse_llm_json("```json\n{bad json\n```", fallback={"approved": True}) == {
        "approved": True
    }


def test_parse_non_string_returns_fallback():
    assert parse_llm_json(None, fallback=[]) == []
