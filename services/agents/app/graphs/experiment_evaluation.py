"""Disabled autonomous experiment evaluator for the OSS developer preview.

Experiment analytics are exposed as read-only, authoritative Query results.
No agent may turn those results into stop, ship, rollback, or iterate mutations
until a separately reviewed decision protocol is implemented.
"""

from __future__ import annotations

from typing import Any

from app.framework.base import BaseAgent
from app.framework.context import AgentContext
from app.framework.registry import register_agent


@register_agent
class ExperimentEvaluationAgent(BaseAgent):
    """Registered but fail-closed autonomous evaluation surface."""

    name = "experiment_evaluation"
    description = "Unavailable in the OSS developer preview: results are read-only."
    enabled = False
    order = 30
    requires = ()
    produces = "experiment_verdicts"
    parse_as = "list"

    async def gather(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
    ) -> dict[str, Any]:
        return {"disabled": True}

    def build_prompt(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
    ) -> None:
        return None

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        return {
            "disabled": True,
            "evaluated": 0,
            "mutations_attempted": 0,
        }
