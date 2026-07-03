"""CustomAgent — a read-only analysis agent hydrated from a ``custom_agents`` row.

Built-in agents are classes; custom agents are data. A definition authored in
the admin console (prompts + tool selection + tier + pipeline position)
becomes a ``CustomAgent`` instance whose attributes shadow the ``BaseAgent``
ClassVars, so the whole Template Method lifecycle — memory retrieval, LLM
call, shape-enforcing parse, supervisor persistence — is reused unchanged.

Deliberate omissions define the v1 safety contract:

- no ``act`` override → no side effects, never ``needs_approval``, so a
  custom agent can never gate a run or reach the deploy paths;
- no ``memory_entries`` override → custom agents read long-term memory
  (``memory_query``) but never write it, keeping the store curated by
  built-ins only;
- ``produces`` may not collide with reserved/built-in state keys (enforced
  by :func:`validate_definition`), so custom output can never feed the gated
  built-in pipeline (e.g. fake ``insights`` driving experiment deployment)
  or corrupt supervisor bookkeeping (``errors``, run counters).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.framework.base import BaseAgent
from app.framework.context import AgentContext
from app.framework.tool_catalog import validate_tool_selection

logger = logging.getLogger(__name__)

#: State keys a custom agent must never produce: supervisor seed keys, keys
#: the run loop reads (``errors``, counter sources), BaseAgent working keys
#: (``context``, ``output``), and every built-in ``produces``.
RESERVED_STATE_KEYS = frozenset(
    {
        "project_id",
        "context",
        "tool_results",
        "output",
        "errors",
        "insights",
        "experiment_designs",
        "personalizations",
        "feature_proposals",
        "changesets",
    }
)

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_MAX_PROMPT = 20_000

#: Placeholders always available to ``user_prompt_template`` (plus one per
#: ``requires`` key).
BASE_PLACEHOLDERS = ("context", "tool_results", "project_id", "time_range_days")


def render_template(template: str, variables: dict[str, str]) -> str:
    """Substitute ``{name}`` placeholders by literal replacement.

    ``str.format`` would raise on the JSON braces prompt authors paste into
    templates ("respond as {\"score\": ...}"); literal replacement leaves
    every unknown brace untouched.
    """
    for key, value in variables.items():
        template = template.replace("{" + key + "}", value)
    return template


class CustomAgent(BaseAgent):
    """A user-defined, read-only analysis agent (see module docstring)."""

    def __init__(self, definition: dict[str, Any]) -> None:
        self.definition = definition
        # Instance attributes shadow the BaseAgent ClassVars — run() reads
        # them via self.*, so the template method works unchanged.
        self.name = definition["slug"]
        self.description = definition.get("description", "")
        self.order = int(definition.get("pipeline_order", 100))
        self.system_prompt = definition["system_prompt"]
        self.model_tier = definition.get("model_tier", "reasoning")
        self.memory_query = definition.get("memory_query") or None
        self.memory_top_k = int(definition.get("memory_top_k", 5))
        self.requires = tuple(definition.get("requires") or ())
        self.produces = definition["produces"]
        self.parse_as = definition.get("parse_as", "object")
        self.user_prompt_template = definition["user_prompt_template"]
        self.tool_specs = list(definition.get("tools") or ())

    def requirements_met(self, state: dict[str, Any]) -> bool:
        """Shadow the BaseAgent *classmethod*, which reads ``cls.requires``.

        The base implementation would consult the empty class default and
        silently ignore this instance's ``requires``.
        """
        return all(state.get(key) for key in self.requires)

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the selected catalog tools sequentially.

        Per-tool failures are captured into the results (and the prompt) so a
        single flaky query degrades the analysis instead of killing the agent
        — mirroring how behavior_analysis isolates its query errors.
        """
        # Import here, not at module top: tests monkeypatch
        # app.framework.tool_catalog.run_tool and late binding honors that.
        from app.framework import tool_catalog

        results: list[dict[str, Any]] = []
        for spec in self.tool_specs:
            name = spec.get("tool")
            params = spec.get("params") or {}
            try:
                output = await tool_catalog.run_tool(ctx, name, params)
                results.append({"tool": name, "params": params, "result": output})
            except Exception as exc:
                logger.error("[%s] custom tool %s failed: %s", self.name, name, exc)
                results.append({"tool": name, "params": params, "error": str(exc)})
        return {"tool_results": results}

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        variables = {
            "context": working.get("context") or "",
            "tool_results": json.dumps(
                working.get("tool_results", []), indent=2, default=str
            ),
            "project_id": ctx.project_id,
            "time_range_days": str(ctx.time_range_days),
        }
        # Upstream outputs this agent declared as requirements are addressable
        # by their state key: requires=["insights"] enables an {insights}
        # placeholder.
        for key in self.requires:
            variables[key] = json.dumps(state.get(key, []), indent=2, default=str)
        return render_template(self.user_prompt_template, variables)


def validate_definition(
    fields: dict[str, Any], builtin_names: set[str], builtin_produces: set[str]
) -> list[str]:
    """Validate a custom agent spec; returns human-readable problems.

    DB-dependent checks (slug uniqueness, produces uniqueness among the
    project's custom agents, custom-to-custom ``requires`` resolution) live in
    the router where a pool is available.
    """
    errors: list[str] = []

    slug = fields.get("slug") or ""
    if not _SLUG_RE.match(slug):
        errors.append(
            "slug must be 3-64 chars of lowercase letters, digits or underscores, "
            "starting with a letter"
        )
    elif slug in builtin_names:
        errors.append(f"slug '{slug}' collides with a built-in agent")

    display_name = fields.get("display_name") or ""
    if not 1 <= len(display_name) <= 120:
        errors.append("display_name must be 1-120 characters")
    if len(fields.get("description") or "") > 500:
        errors.append("description must be at most 500 characters")

    for key in ("system_prompt", "user_prompt_template"):
        value = fields.get(key) or ""
        if not 1 <= len(value) <= _MAX_PROMPT:
            errors.append(f"{key} must be 1-{_MAX_PROMPT} characters")

    if fields.get("model_tier") not in ("fast", "reasoning"):
        errors.append("model_tier must be 'fast' or 'reasoning'")
    if fields.get("parse_as") not in ("object", "list"):
        errors.append("parse_as must be 'object' or 'list'")

    memory_query = fields.get("memory_query")
    if memory_query is not None and len(memory_query) > 500:
        errors.append("memory_query must be at most 500 characters")
    memory_top_k = fields.get("memory_top_k", 5)
    if not isinstance(memory_top_k, int) or not 1 <= memory_top_k <= 20:
        errors.append("memory_top_k must be an integer between 1 and 20")
    pipeline_order = fields.get("pipeline_order", 100)
    if not isinstance(pipeline_order, int) or not 0 <= pipeline_order <= 1000:
        errors.append("pipeline_order must be an integer between 0 and 1000")

    tools = fields.get("tools") or []
    if not isinstance(tools, list) or len(tools) > 8:
        errors.append("tools must be a list of at most 8 selections")
    else:
        try:
            validate_tool_selection(tools)
        except ValueError as exc:
            errors.append(str(exc))

    requires = fields.get("requires") or []
    if not isinstance(requires, list) or len(requires) > 5:
        errors.append("requires must be a list of at most 5 state keys")

    produces = fields.get("produces") or ""
    if not _KEY_RE.match(produces):
        errors.append(
            "produces must be 3-64 chars of lowercase letters, digits or underscores, "
            "starting with a letter"
        )
    elif produces in RESERVED_STATE_KEYS or produces in builtin_produces:
        errors.append(f"produces '{produces}' is a reserved state key")

    return errors
