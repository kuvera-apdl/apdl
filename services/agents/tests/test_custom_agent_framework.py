"""CustomAgent: definition hydration, template rendering, agentic-tools wiring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.framework import custom as custom_mod
from app.framework.custom import (
    RESERVED_STATE_KEYS,
    CustomAgent,
    render_preset_results,
    render_template,
    validate_definition,
)
from app.framework.tool_catalog import TOOL_CATALOG
from app.framework.tool_loop import ToolTraceEntry


def _definition(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "Watches churn signals",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Context: {context}\nProject: {project_id}",
        "model_tier": "fast",
        "tools": ["discover_events", "query_events"],
        "requires": [],
        "produces": "churn_signals",
        "memory_query": None,
        "memory_top_k": 3,
        "pipeline_order": 60,
        "max_tool_steps": 6,
    }
    base.update(overrides)
    return base


def _ctx(**overrides: Any) -> Any:
    base = {"project_id": "demo", "time_range_days": 7}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_instance_attrs_shadow_classvars():
    agent = CustomAgent(_definition())
    assert agent.name == "churn_watch"
    assert agent.order == 60
    assert agent.model_tier == "fast"
    assert agent.produces == "churn_signals"
    assert agent.parse_as == "list"
    assert agent.memory_top_k == 3
    assert agent.requires == ()
    assert agent.agentic_tools == ("discover_events", "query_events")
    assert agent.max_tool_steps == 6


def test_empty_tools_selection_allows_whole_catalog():
    # The wizard default: no explicit narrowing means every catalog tool.
    agent = CustomAgent(_definition(tools=[]))
    assert agent.agentic_tools == tuple(TOOL_CATALOG)


def test_legacy_tool_dict_entries_are_ignored_defensively():
    # Pre-agentic rows stored {"tool": ..., "params": ...}; the store
    # normalizes them, but hydration must not crash if one slips through.
    agent = CustomAgent(_definition(tools=[{"tool": "discover_events"}]))
    assert agent.agentic_tools == tuple(TOOL_CATALOG)


def test_requirements_met_honors_instance_requires():
    # BaseAgent.requirements_met is a *classmethod* reading cls.requires;
    # without the instance-method shadow, instance requires would be silently
    # ignored and this agent would run with missing inputs.
    agent = CustomAgent(_definition(requires=["insights"]))
    assert agent.requirements_met({"insights": []}) is False
    assert agent.requirements_met({"insights": [{"a": 1}]}) is True


def test_render_template_leaves_json_braces_intact():
    template = 'Respond as {"score": 0.5, "why": "..."} given {context}'
    out = render_template(template, {"context": "[1, 2]"})
    assert out == 'Respond as {"score": 0.5, "why": "..."} given [1, 2]'


def test_build_prompt_substitutes_base_and_requires_placeholders():
    agent = CustomAgent(
        _definition(
            requires=["insights"],
            user_prompt_template="P={project_id} D={time_range_days} I={insights}",
        )
    )
    state = {"insights": [{"finding": "drop"}]}
    working = {"context": ""}
    prompt = agent.build_prompt(_ctx(), state, working)
    assert "P=demo" in prompt
    assert "D=7" in prompt
    assert '"finding": "drop"' in prompt


def test_build_prompt_blanks_tool_results_placeholder_without_presets():
    # Without preset tools {tool_results} renders empty (pre-agentic templates
    # interpolated it, so it must never leak literally).
    agent = CustomAgent(_definition(user_prompt_template="Data: {tool_results}!"))
    assert agent.build_prompt(_ctx(), {}, {"context": ""}) == "Data: !"


# --- preset (deterministic) tools --------------------------------------------


def _preset_definition(**overrides: Any) -> dict[str, Any]:
    return _definition(
        preset_tools=[{"tool": "discover_events", "params": {"limit": 50}}],
        **overrides,
    )


def test_preset_tools_hydration_is_defensive():
    agent = CustomAgent(
        _definition(
            preset_tools=[
                {"tool": "list_flags"},
                {"tool": "query_events", "params": "junk"},
                "bogus",
                {"params": {}},
            ]
        )
    )
    assert agent.preset_tools == [
        {"tool": "list_flags", "params": {}},
        {"tool": "query_events", "params": {}},
    ]


@pytest.mark.asyncio
async def test_gather_without_presets_is_a_noop():
    agent = CustomAgent(_definition())
    assert await agent.gather(_ctx(), {}, {}) == {}


@pytest.mark.asyncio
async def test_gather_runs_presets_through_the_catalog(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_run_tool(ctx: Any, name: str, params: dict[str, Any]) -> Any:
        calls.append((name, params))
        return {"events": ["signup"]}

    monkeypatch.setattr("app.framework.tool_catalog.run_tool", fake_run_tool)
    agent = CustomAgent(_preset_definition())
    ctx = _ctx(audit=SimpleNamespace(log=_async_noop), run_id="run-1")

    working = await agent.gather(ctx, {}, {})

    assert calls == [("discover_events", {"limit": 50})]
    trace = working["tool_results"]
    assert len(trace) == 1
    assert trace[0].tool == "discover_events"
    assert '"events"' in trace[0].result


def test_build_prompt_renders_preset_results_into_placeholder():
    agent = CustomAgent(
        _preset_definition(user_prompt_template="Baseline:\n{tool_results}\nGo.")
    )
    trace = [
        ToolTraceEntry(
            tool="discover_events", params={"limit": 50}, result='{"events": 3}'
        )
    ]
    prompt = agent.build_prompt(_ctx(), {}, {"context": "", "tool_results": trace})
    assert '### discover_events {"limit": 50}' in prompt
    assert '{"events": 3}' in prompt
    assert "{tool_results}" not in prompt


def test_build_prompt_appends_preset_results_when_placeholder_missing():
    # Determinism contract: preset data reaches the model even if the author
    # never placed {tool_results} in the template.
    agent = CustomAgent(_preset_definition(user_prompt_template="Just analyse."))
    trace = [ToolTraceEntry(tool="list_flags", params={}, result="[]")]
    prompt = agent.build_prompt(_ctx(), {}, {"context": "", "tool_results": trace})
    assert prompt.startswith("Just analyse.")
    assert "## Preset data (gathered automatically)" in prompt
    assert "### list_flags {}" in prompt


def test_preset_errors_render_as_error_blocks():
    trace = [
        ToolTraceEntry(tool="query_funnel", params={"steps": []}, error="ValueError: bad"),
        ToolTraceEntry(tool="list_flags", params={}, result="[]"),
    ]
    rendered = render_preset_results(trace)
    assert "ERROR: ValueError: bad" in rendered
    assert "### list_flags {}\n[]" in rendered


async def _async_noop(*args: Any, **kwargs: Any) -> None:
    return None


def test_parse_enforces_list_shape():
    agent = CustomAgent(_definition())
    assert agent.parse('{"a": 1}') == [{"a": 1}]
    assert agent.parse("[1, 2]") == []  # non-dict items dropped
    assert agent.parse('[{"a": 1}]') == [{"a": 1}]


def test_custom_agent_has_no_side_effect_hooks():
    # The safety contract: no act/memory_entries overrides means no deploy
    # paths and no memory writes, so a custom agent can never gate a run. The
    # gather override is data collection only — it executes preset calls
    # through the same read-only, ctx-scoped catalog boundary as the tool loop.
    assert "act" not in CustomAgent.__dict__
    assert "memory_entries" not in CustomAgent.__dict__


# --- validate_definition ----------------------------------------------------

_BUILTINS = {"behavior_analysis", "experiment_design"}
_BUILTIN_PRODUCES = {"insights", "experiment_designs"}


def _validate(**overrides: Any) -> list[str]:
    return validate_definition(_definition(**overrides), _BUILTINS, _BUILTIN_PRODUCES)


def test_valid_definition_passes():
    assert _validate() == []


def test_slug_rules():
    assert any("slug" in e for e in _validate(slug="Bad-Slug"))
    assert any("collides with a built-in" in e for e in _validate(slug="behavior_analysis"))


def test_produces_must_not_be_reserved():
    for key in ("insights", "errors", "context", "tool_results", "tool_trace", "changesets"):
        assert key in RESERVED_STATE_KEYS or key in _BUILTIN_PRODUCES
        assert any("reserved" in e for e in _validate(produces=key)), key


def test_prompt_length_limits():
    assert any("system_prompt" in e for e in _validate(system_prompt=""))
    assert any(
        "user_prompt_template" in e
        for e in _validate(user_prompt_template="x" * (custom_mod._MAX_PROMPT + 1))
    )


def test_tools_validated_against_catalog():
    errors = _validate(tools=["create_flag"])
    assert any("unknown tool" in e for e in errors)
    # Legacy dict entries are no longer a valid spec shape.
    errors = _validate(tools=[{"tool": "discover_events", "params": {}}])
    assert any("unknown tool" in e for e in errors)


def test_preset_tools_validated_against_catalog_and_param_schemas():
    # Presets execute verbatim on every run — bad params must fail at
    # authoring time, since no model can correct them at run time.
    assert _validate(preset_tools=[{"tool": "discover_events", "params": {"limit": 5}}]) == []
    assert any(
        "unknown tool" in e
        for e in _validate(preset_tools=[{"tool": "create_flag", "params": {}}])
    )
    assert any(
        "preset_tools[0]" in e
        for e in _validate(preset_tools=[{"tool": "discover_events", "params": {"limit": 0}}])
    )
    assert any(
        "preset_tools[0]" in e
        for e in _validate(preset_tools=[{"tool": "list_flags", "params": {"extra": 1}}])
    )
    assert any(
        "at most" in e
        for e in _validate(preset_tools=[{"tool": "list_flags", "params": {}}] * 11)
    )


def test_bounds():
    assert any("memory_top_k" in e for e in _validate(memory_top_k=0))
    assert any("pipeline_order" in e for e in _validate(pipeline_order=-1))
    assert any("model_tier" in e for e in _validate(model_tier="turbo"))
    assert any("requires" in e for e in _validate(requires=["a", "b", "c", "d", "e", "f"]))
    assert any("max_tool_steps" in e for e in _validate(max_tool_steps=0))
    assert any("max_tool_steps" in e for e in _validate(max_tool_steps=99))
