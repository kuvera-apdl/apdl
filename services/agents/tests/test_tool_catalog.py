"""Tool catalog: the read-only allowlist, param validation, ctx injection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.framework import tool_catalog


def _ctx(project_id: str = "demo", days: int = 7) -> Any:
    return SimpleNamespace(project_id=project_id, time_range_days=days)


def test_catalog_is_read_only():
    # The catalog is the security boundary: nothing that mutates state may
    # ever appear here.
    forbidden = {"create", "update", "merge", "open", "deploy"}
    for name in tool_catalog.TOOL_CATALOG:
        assert not any(name.startswith(verb) for verb in forbidden), name


def test_every_entry_has_valid_defaults_or_requires_params():
    for name, spec in tool_catalog.TOOL_CATALOG.items():
        schema = spec.params_model.model_json_schema()
        assert spec.description
        assert isinstance(schema, dict), name


def test_validate_tool_selection_normalizes_params():
    out = tool_catalog.validate_tool_selection(
        [{"tool": "discover_events", "params": {"limit": 10}}, {"tool": "list_flags"}]
    )
    assert out == [
        {"tool": "discover_events", "params": {"limit": 10}},
        {"tool": "list_flags", "params": {}},
    ]


def test_validate_tool_selection_rejects_unknown_tool():
    with pytest.raises(ValueError, match="unknown tool 'create_flag'"):
        tool_catalog.validate_tool_selection([{"tool": "create_flag", "params": {}}])


def test_validate_tool_selection_aggregates_param_errors():
    with pytest.raises(ValueError) as exc:
        tool_catalog.validate_tool_selection(
            [
                # A funnel needs at least two steps.
                {"tool": "query_funnel", "params": {"steps": [{"event_name": "a"}]}},
                # Interval is a strict literal.
                {
                    "tool": "query_timeseries",
                    "params": {"selector": {"event_name": "a"}, "interval": "1 YEAR"},
                },
            ]
        )
    message = str(exc.value)
    assert "tools[0] (query_funnel)" in message
    assert "tools[1] (query_timeseries)" in message


def test_validate_tool_selection_rejects_extra_params():
    # extra="forbid": definition-supplied project_id must never sneak through.
    with pytest.raises(ValueError, match="project_id"):
        tool_catalog.validate_tool_selection(
            [{"tool": "discover_events", "params": {"project_id": "other"}}]
        )


@pytest.mark.asyncio
async def test_run_tool_injects_project_and_date_window(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_funnel(**kwargs):
        captured.update(kwargs)
        return {"steps": []}

    monkeypatch.setattr(tool_catalog.clickhouse, "query_funnel", fake_funnel)

    await tool_catalog.run_tool(
        _ctx(project_id="proj-1", days=14),
        "query_funnel",
        {"steps": [{"event_name": "signup"}, {"event_name": "purchase"}], "window_days": 3},
    )

    assert captured["project_id"] == "proj-1"
    assert captured["window_days"] == 3
    assert captured["steps"][0] == {"event_name": "signup", "filters": []}
    # The window is ctx-derived: 14 days wide, ISO dates.
    from datetime import date

    start = date.fromisoformat(captured["start_date"])
    end = date.fromisoformat(captured["end_date"])
    assert (end - start).days == 14


@pytest.mark.asyncio
async def test_run_tool_scopes_config_service_tools_to_ctx_project(monkeypatch):
    seen: list[str] = []

    async def fake_flags(project_id):
        seen.append(project_id)
        return []

    monkeypatch.setattr(tool_catalog, "get_active_flags", fake_flags)
    await tool_catalog.run_tool(_ctx(project_id="proj-2"), "list_flags", {})
    assert seen == ["proj-2"]


@pytest.mark.asyncio
async def test_run_tool_rejects_unknown_name():
    with pytest.raises(ValueError, match="Unknown tool"):
        await tool_catalog.run_tool(_ctx(), "drop_tables", {})


def test_catalog_descriptions_expose_json_schemas():
    catalog = tool_catalog.catalog_descriptions()
    by_name = {entry["name"]: entry for entry in catalog}
    assert set(by_name) == set(tool_catalog.TOOL_CATALOG)
    funnel_schema = by_name["query_funnel"]["params_schema"]
    assert "steps" in funnel_schema["properties"]
