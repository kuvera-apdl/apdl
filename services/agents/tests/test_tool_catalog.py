"""Tool catalog: the read-only allowlist, param validation, ctx injection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

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


def test_validate_tool_names_normalizes_to_catalog_order():
    out = tool_catalog.validate_tool_names(["list_flags", "discover_events", "list_flags"])
    # Deduplicated, and returned in catalog order regardless of author order.
    assert out == ["discover_events", "list_flags"]


def test_validate_tool_names_rejects_unknown_tool():
    # The catalog stays the security boundary: mutating tools can never be allowed.
    with pytest.raises(ValueError, match="unknown tool 'create_flag'"):
        tool_catalog.validate_tool_names(["create_flag"])


def test_catalog_excludes_undeliverable_ui_config_tool():
    assert "list_ui_configs" not in tool_catalog.TOOL_CATALOG
    with pytest.raises(ValueError, match="unknown tool 'list_ui_configs'"):
        tool_catalog.validate_tool_names(["list_ui_configs"])


def test_validate_tool_names_aggregates_errors():
    with pytest.raises(ValueError) as exc:
        tool_catalog.validate_tool_names(["nope", {"tool": "discover_events"}])
    message = str(exc.value)
    assert "tools[0]" in message
    assert "tools[1]" in message


def test_llm_tool_schemas_shape():
    schemas = tool_catalog.llm_tool_schemas(["query_funnel", "list_flags"])
    assert [s["name"] for s in schemas] == ["query_funnel", "list_flags"]
    assert all(s["description"] for s in schemas)
    assert "steps" in schemas[0]["parameters"]["properties"]


def test_llm_tool_schemas_rejects_unknown():
    with pytest.raises(ValueError, match="Unknown tool"):
        tool_catalog.llm_tool_schemas(["drop_tables"])


@pytest.mark.asyncio
async def test_run_tool_rejects_definition_supplied_project_id(monkeypatch):
    # extra="forbid": model-supplied project_id must never sneak through.
    with pytest.raises(Exception, match="project_id"):
        await tool_catalog.run_tool(
            _ctx(), "discover_events", {"project_id": "other"}
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
async def test_retention_tool_requires_and_forwards_window_relative_mode(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_retention(**kwargs):
        captured.update(kwargs)
        return {"cohort_mode": "first_match_in_window", "cohorts": []}

    monkeypatch.setattr(tool_catalog.clickhouse, "query_retention", fake_retention)
    params = {
        "cohort_selector": {"event_name": "signup"},
        "return_selector": {"event_name": "login"},
        "cohort_mode": "first_match_in_window",
    }

    await tool_catalog.run_tool(_ctx(project_id="proj-1"), "query_retention", params)

    assert captured["project_id"] == "proj-1"
    assert captured["cohort_mode"] == "first_match_in_window"

    with pytest.raises(ValidationError):
        await tool_catalog.run_tool(
            _ctx(),
            "query_retention",
            {key: value for key, value in params.items() if key != "cohort_mode"},
        )
    with pytest.raises(ValidationError):
        await tool_catalog.run_tool(
            _ctx(),
            "query_retention",
            {**params, "cohort_mode": "all_history"},
        )


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
async def test_experiment_planner_tool_returns_the_canonical_plan(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_plan(**kwargs):
        captured.update(kwargs)
        return {"protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1"}

    monkeypatch.setattr(tool_catalog, "calculate_sample_size", fake_plan)
    result = await tool_catalog.run_tool(
        _ctx(),
        "calculate_statistical_plan",
        {
            "baseline_conversion_rate": 0.5,
            "minimum_detectable_effect": 0.5,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "treatment_count": 2,
            "direction": "increase",
            "data_settlement_seconds": 300,
        },
    )

    assert result["protocol"] == "fixed_horizon_fisher_newcombe_cc_plan_v1"
    assert captured == {
        "baseline_rate": 0.5,
        "minimum_detectable_effect": 0.5,
        "alpha": 0.05,
        "nominal_power": 0.8,
        "treatment_count": 2,
        "direction": "increase",
        "data_settlement_seconds": 300,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_conversion_rate", "0.5"),
        ("treatment_count", 2.0),
        ("data_settlement_seconds", "300"),
    ],
)
async def test_experiment_planner_tool_rejects_numeric_coercion(field, value):
    params = {
        "baseline_conversion_rate": 0.5,
        "minimum_detectable_effect": 0.1,
        "significance_level": 0.05,
        "nominal_power": 0.8,
        "treatment_count": 2,
        "direction": "increase",
        "data_settlement_seconds": 300,
    }
    params[field] = value

    with pytest.raises(ValidationError):
        await tool_catalog.run_tool(_ctx(), "calculate_statistical_plan", params)


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
