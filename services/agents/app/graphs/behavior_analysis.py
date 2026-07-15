"""Behavior analysis agent.

Investigates the project's event data agentically: the reasoning model drives
the read-only query catalog itself (discover events, then funnels, timeseries,
retention, cohorts, breakdowns — following up on what it finds) inside the
framework's bounded tool loop, and synthesizes what it observed into
actionable insights. Produces the ``insights`` consumed by the
experiment-design, personalization, and feature-proposal agents.

This replaced the earlier plan-then-execute pipeline (one up-front query plan,
then one synthesis call): the open-loop plan could never react to results —
a funnel drop-off could not be drilled into — which is exactly what the tool
loop exists to do.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.analysis import BEHAVIOR_ANALYSIS_SYSTEM, INVESTIGATION_PROMPT
from app.llm.utils import parse_llm_json

logger = logging.getLogger(__name__)


@register_agent
class BehaviorAnalysisAgent(BaseAgent):
    """Investigates user behaviour with query tools and emits prioritised insights."""

    name = "behavior_analysis"
    description = "Investigate analytics data with query tools and synthesise insights."
    order = 10
    system_prompt = BEHAVIOR_ANALYSIS_SYSTEM
    model_tier = "reasoning"
    memory_query = "recent behavior analysis insights anomalies trends"
    memory_top_k = 5
    produces = "insights"
    parse_as = "list"
    agentic_tools = (
        "discover_events",
        "query_events",
        "query_timeseries",
        "query_funnel",
        "query_retention",
        "query_cohort",
        "query_breakdown",
    )
    #: The investigator agent gets the deepest budget in the pipeline — it is
    #: the primary data-discovery pass everything downstream builds on.
    max_tool_steps = 10

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        return INVESTIGATION_PROMPT.format(
            project_id=ctx.project_id,
            time_range_days=ctx.time_range_days,
            context=working.get("context", ""),
        )

    def parse(self, response: str) -> Any:
        # Unparseable output means no insights — not a fabricated "Raw
        # analysis" pseudo-insight, which would satisfy downstream
        # requires=("insights",), get stored to vector memory, and could seed
        # an experiment design off unparsed prose.
        insights = parse_llm_json(response, None)
        if insights is None:
            logger.warning("Investigation output was unparseable; producing no insights")
            return []
        if not isinstance(insights, list):
            insights = [insights]
        return [i for i in insights if isinstance(i, dict)]

    def memory_entries(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
        action: dict[str, Any],
    ) -> list[MemoryEntry]:
        return [
            MemoryEntry(
                content=json.dumps(insight, default=str),
                metadata={
                    "type": "behavior_insight",
                    "title": insight.get("title", ""),
                    "confidence": insight.get("confidence", "unknown"),
                },
            )
            for insight in output
        ]
