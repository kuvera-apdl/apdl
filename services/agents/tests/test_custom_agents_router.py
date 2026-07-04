"""Custom agents CRUD router: validation matrix, status codes, route order."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import custom_agents as router_mod
from app.store.custom_agents import SlugConflictError


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "Watches churn signals",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Analyse churn for {project_id}",
        "model_tier": "fast",
        "tools": ["discover_events", "query_events"],
        "requires": [],
        "produces": "churn_signals",
        "parse_as": "list",
        "memory_query": None,
        "memory_top_k": 5,
        "pipeline_order": 60,
        "max_tool_steps": 8,
    }
    base.update(overrides)
    return base


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "agent_id": "agent-1",
        "project_id": "demo",
        "status": "active",
        "created_at": "2026-07-02T00:00:00Z",
        "updated_at": "2026-07-02T00:00:00Z",
        **_spec(),
    }
    row.update(overrides)
    return row


def _client() -> AsyncClient:
    app.state.pg_pool = object()
    app.state.vector_store = object()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def stubbed_store(monkeypatch):
    """Stub the store layer in the router namespace; capture calls."""
    calls: dict[str, Any] = {"created": None, "updated": None, "archived": None}

    async def fake_list(pool, project_id, include_archived=False):
        return list(calls.get("existing") or [])

    async def fake_create(pool, project_id, fields):
        calls["created"] = (project_id, fields)
        return _row(**{k: v for k, v in fields.items()})

    async def fake_get(pool, agent_id):
        return calls.get("get_result")

    async def fake_update(pool, agent_id, fields):
        calls["updated"] = (agent_id, fields)
        return _row(**{k: v for k, v in fields.items()})

    async def fake_archive(pool, agent_id):
        calls["archived"] = agent_id
        return True

    monkeypatch.setattr(router_mod, "list_custom_agents", fake_list)
    monkeypatch.setattr(router_mod, "create_custom_agent", fake_create)
    monkeypatch.setattr(router_mod, "get_custom_agent", fake_get)
    monkeypatch.setattr(router_mod, "update_custom_agent", fake_update)
    monkeypatch.setattr(router_mod, "archive_custom_agent", fake_archive)
    return calls


@pytest.mark.asyncio
async def test_create_happy_path(stubbed_store):
    async with _client() as client:
        resp = await client.post("/v1/agents/custom?project_id=demo", json=_spec())
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "churn_watch"
    assert body["agent_id"] == "agent-1"
    project_id, fields = stubbed_store["created"]
    assert project_id == "demo"
    assert fields["produces"] == "churn_signals"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": "behavior_analysis"}, "built-in"),
        ({"produces": "insights"}, "reserved"),
        ({"produces": "errors"}, "reserved"),
        ({"tools": ["create_flag"]}, "unknown tool"),
        ({"max_tool_steps": 0}, "max_tool_steps"),
        ({"requires": ["no_such_output"]}, "does not match"),
        ({"model_tier": "turbo"}, "model_tier"),
    ],
)
async def test_create_validation_matrix(stubbed_store, overrides, fragment):
    async with _client() as client:
        resp = await client.post("/v1/agents/custom?project_id=demo", json=_spec(**overrides))
    assert resp.status_code == 422
    assert fragment in resp.json()["detail"]
    assert stubbed_store["created"] is None


@pytest.mark.asyncio
async def test_create_rejects_produces_collision_with_sibling(stubbed_store):
    stubbed_store["existing"] = [_row(agent_id="other", slug="other_agent")]
    async with _client() as client:
        resp = await client.post("/v1/agents/custom?project_id=demo", json=_spec())
    assert resp.status_code == 422
    assert "already used by another custom agent" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_requires_may_reference_sibling_produces(stubbed_store):
    stubbed_store["existing"] = [
        _row(agent_id="other", slug="other_agent", produces="other_signals")
    ]
    async with _client() as client:
        resp = await client.post(
            "/v1/agents/custom?project_id=demo",
            json=_spec(requires=["other_signals", "insights"]),
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_slug_conflict_maps_to_409(stubbed_store, monkeypatch):
    async def conflicting_create(pool, project_id, fields):
        raise SlugConflictError("slug taken")

    monkeypatch.setattr(router_mod, "create_custom_agent", conflicting_create)
    async with _client() as client:
        resp = await client.post("/v1/agents/custom?project_id=demo", json=_spec())
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_scopes_by_project(stubbed_store):
    stubbed_store["get_result"] = _row(project_id="someone_else")
    async with _client() as client:
        resp = await client.get("/v1/agents/custom/agent-1?project_id=demo")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_and_archive(stubbed_store):
    stubbed_store["get_result"] = _row()
    async with _client() as client:
        resp = await client.put(
            "/v1/agents/custom/agent-1?project_id=demo",
            json=_spec(display_name="Renamed"),
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Renamed"

        resp = await client.delete("/v1/agents/custom/agent-1?project_id=demo")
        assert resp.status_code == 204
        assert stubbed_store["archived"] == "agent-1"


@pytest.mark.asyncio
async def test_list_custom_agents_endpoint(stubbed_store):
    stubbed_store["existing"] = [_row()]
    async with _client() as client:
        resp = await client.get("/v1/agents/custom?project_id=demo")
    assert resp.status_code == 200
    assert [a["slug"] for a in resp.json()] == ["churn_watch"]


@pytest.mark.asyncio
async def test_definitions_merges_builtins_and_custom_sorted_by_order(stubbed_store):
    stubbed_store["existing"] = [_row(pipeline_order=15)]
    async with _client() as client:
        resp = await client.get("/v1/agents/definitions?project_id=demo")
    assert resp.status_code == 200
    body = resp.json()
    names = [a["name"] for a in body["agents"]]
    # behavior_analysis (order 10) < custom (15) < experiment_design (20).
    assert names.index("behavior_analysis") < names.index("churn_watch")
    assert names.index("churn_watch") < names.index("experiment_design")
    custom = next(a for a in body["agents"] if a["name"] == "churn_watch")
    assert custom["is_custom"] is True and custom["agent_id"] == "agent-1"
    builtin = next(a for a in body["agents"] if a["name"] == "behavior_analysis")
    assert builtin["is_custom"] is False
    assert {t["name"] for t in body["tool_catalog"]} >= {"discover_events", "query_funnel"}


@pytest.mark.asyncio
async def test_custom_routes_win_over_run_id_wildcards(stubbed_store):
    """Route-order regression: /custom and /definitions must not be swallowed
    by the run routers' GET /{run_id}/... shapes."""
    async with _client() as client:
        resp = await client.get("/v1/agents/custom?project_id=demo")
        assert resp.status_code == 200  # not a 404 from a runs lookup
        resp = await client.get("/v1/agents/definitions?project_id=demo")
        assert resp.status_code == 200
