"""Regression tests for the agent-run failures found in the admin console.

Covers: behavior-agent event discovery, event_count sending selectors (not the
bad event_names kwarg), feature_proposal None-safety, and parse_llm_json never
raising on malformed safety-review responses.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.framework import AgentContext
from app.graphs import behavior_analysis as ba
from app.graphs import feature_proposal as fp
from app.graphs.behavior_analysis import BehaviorAnalysisAgent, _format_event_catalog
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
# Event catalog formatting / discovery
# ---------------------------------------------------------------------------

def test_format_event_catalog_lists_events():
    out = _format_event_catalog(
        [{"event_name": "page", "event_count": 57, "unique_users": 3}]
    )
    assert "page" in out
    assert "57" in out


def test_format_event_catalog_empty_warns_not_to_fabricate():
    out = _format_event_catalog([])
    assert "no events" in out.lower()


@pytest.mark.asyncio
async def test_discover_events_returns_catalog(monkeypatch):
    async def fake_discover(**kwargs):
        return {"events": [{"event_name": "page", "event_count": 57}]}

    monkeypatch.setattr(ba, "discover_events", fake_discover)
    catalog = await BehaviorAnalysisAgent()._discover_events(_ctx())
    assert catalog == [{"event_name": "page", "event_count": 57}]


@pytest.mark.asyncio
async def test_discover_events_swallows_errors(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("query service down")

    monkeypatch.setattr(ba, "discover_events", boom)
    assert await BehaviorAnalysisAgent()._discover_events(_ctx()) == []


# ---------------------------------------------------------------------------
# event_count must send `selectors`, never the unsupported `event_names` kwarg
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_count_sends_selectors(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_query_events(**kwargs):
        captured.update(kwargs)
        return {"results": []}

    monkeypatch.setattr(ba, "query_events", fake_query_events)
    results = await BehaviorAnalysisAgent()._run_queries(
        _ctx(),
        [{"type": "event_count", "selectors": [{"event_name": "page", "filters": []}]}],
    )
    assert captured.get("selectors") == [{"event_name": "page", "filters": []}]
    assert "event_names" not in captured
    assert results[0].get("error") is None


@pytest.mark.asyncio
async def test_event_count_accepts_legacy_event_names(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_query_events(**kwargs):
        captured.update(kwargs)
        return {"results": []}

    monkeypatch.setattr(ba, "query_events", fake_query_events)
    await BehaviorAnalysisAgent()._run_queries(
        _ctx(), [{"type": "event_count", "event_names": ["page", "$click"]}]
    )
    assert captured["selectors"] == [
        {"event_name": "page", "filters": []},
        {"event_name": "$click", "filters": []},
    ]


# ---------------------------------------------------------------------------
# feature_proposal must not crash on an experiment with a null primary_metric
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feature_proposal_handles_null_primary_metric(monkeypatch):
    async def fake_active(**kwargs):
        return [{"experiment_id": "e1", "primary_metric": None}]

    async def fake_results(**kwargs):
        return {}

    monkeypatch.setattr(fp, "get_active_experiments", fake_active)
    monkeypatch.setattr(fp, "get_experiment_results", fake_results)

    out = await fp.FeatureProposalAgent().gather(_ctx(), {}, {})
    # The null-metric experiment is skipped, not crashed on.
    assert out["experiment_results"] == []
    assert out["active_experiments"] == [{"experiment_id": "e1", "primary_metric": None}]


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
