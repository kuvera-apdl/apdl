"""CustomAgent: definition hydration, template rendering, gather isolation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.framework import custom as custom_mod
from app.framework import tool_catalog
from app.framework.custom import (
    RESERVED_STATE_KEYS,
    CustomAgent,
    render_template,
    validate_definition,
)


def _definition(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slug": "churn_watch",
        "display_name": "Churn watch",
        "description": "Watches churn signals",
        "system_prompt": "You are a churn analyst.",
        "user_prompt_template": "Context: {context}\nData: {tool_results}",
        "model_tier": "fast",
        "tools": [{"tool": "discover_events", "params": {"limit": 50}}],
        "requires": [],
        "produces": "churn_signals",
        "parse_as": "list",
        "memory_query": None,
        "memory_top_k": 3,
        "pipeline_order": 60,
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


def test_requirements_met_honors_instance_requires():
    # BaseAgent.requirements_met is a *classmethod* reading cls.requires;
    # without the instance-method shadow, instance requires would be silently
    # ignored and this agent would run with missing inputs.
    agent = CustomAgent(_definition(requires=["insights"]))
    assert agent.requirements_met({"insights": []}) is False
    assert agent.requirements_met({"insights": [{"a": 1}]}) is True


def test_render_template_leaves_json_braces_intact():
    template = 'Respond as {"score": 0.5, "why": "..."} given {tool_results}'
    out = render_template(template, {"tool_results": "[1, 2]"})
    assert out == 'Respond as {"score": 0.5, "why": "..."} given [1, 2]'


def test_build_prompt_substitutes_base_and_requires_placeholders():
    agent = CustomAgent(
        _definition(
            requires=["insights"],
            user_prompt_template="P={project_id} D={time_range_days} I={insights} T={tool_results}",
        )
    )
    state = {"insights": [{"finding": "drop"}]}
    working = {"context": "", "tool_results": [{"tool": "x", "result": 1}]}
    prompt = agent.build_prompt(_ctx(), state, working)
    assert "P=demo" in prompt
    assert "D=7" in prompt
    assert '"finding": "drop"' in prompt
    assert '"tool": "x"' in prompt


@pytest.mark.asyncio
async def test_gather_captures_per_tool_errors_without_raising(monkeypatch):
    async def flaky_run_tool(ctx, name, params):
        if name == "discover_events":
            raise RuntimeError("warehouse down")
        return {"ok": True}

    monkeypatch.setattr(tool_catalog, "run_tool", flaky_run_tool)
    agent = CustomAgent(
        _definition(
            tools=[
                {"tool": "discover_events", "params": {}},
                {"tool": "list_flags", "params": {}},
            ]
        )
    )
    out = await agent.gather(_ctx(), {}, {})
    results = out["tool_results"]
    assert results[0]["error"] == "warehouse down"
    assert results[1]["result"] == {"ok": True}


def test_parse_enforces_list_shape():
    agent = CustomAgent(_definition(parse_as="list"))
    assert agent.parse('{"a": 1}') == [{"a": 1}]
    assert agent.parse("[1, 2]") == []  # non-dict items dropped
    assert agent.parse('[{"a": 1}]') == [{"a": 1}]


def test_custom_agent_has_no_side_effect_hooks():
    # The v1 safety contract: no act/memory_entries overrides means no deploy
    # paths and no memory writes, so a custom agent can never gate a run.
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
    for key in ("insights", "errors", "context", "tool_results", "changesets"):
        assert key in RESERVED_STATE_KEYS or key in _BUILTIN_PRODUCES
        assert any("reserved" in e for e in _validate(produces=key)), key


def test_prompt_length_limits():
    assert any("system_prompt" in e for e in _validate(system_prompt=""))
    assert any(
        "user_prompt_template" in e
        for e in _validate(user_prompt_template="x" * (custom_mod._MAX_PROMPT + 1))
    )


def test_tools_validated_against_catalog():
    errors = _validate(tools=[{"tool": "create_flag", "params": {}}])
    assert any("unknown tool" in e for e in errors)


def test_bounds():
    assert any("memory_top_k" in e for e in _validate(memory_top_k=0))
    assert any("pipeline_order" in e for e in _validate(pipeline_order=-1))
    assert any("model_tier" in e for e in _validate(model_tier="turbo"))
    assert any("parse_as" in e for e in _validate(parse_as="text"))
    assert any("requires" in e for e in _validate(requires=["a", "b", "c", "d", "e", "f"]))
