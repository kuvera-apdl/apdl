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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.framework.tool_loop import (
    ToolLoopResult,
    ToolTraceEntry,
    tool_result_source_id,
)
from app.llm.prompts.analysis import BEHAVIOR_ANALYSIS_SYSTEM, INVESTIGATION_PROMPT

logger = logging.getLogger(__name__)


class InsightEvidence(BaseModel):
    """Grounding references for a behavior insight."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2_000)
    source_ids: list[str] = Field(min_length=1, max_length=20)

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("source_ids must be unique")
        if any(
            not value.startswith("warehouse:")
            or len(value) != len("warehouse:") + 24
            for value in values
        ):
            raise ValueError("source_ids must use canonical warehouse IDs")
        return values


class BehaviorInsight(BaseModel):
    """Strict warehouse-grounded output contract for behavior analysis."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4_000)
    evidence: InsightEvidence
    confidence: Literal["high", "medium", "low"]
    impact: Literal["high", "medium", "low"]
    recommended_action: str = Field(min_length=1, max_length=4_000)
    action_type: Literal[
        "experiment", "deeper_analysis", "immediate_fix", "monitor"
    ]


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

    def agentic_terminal_result(self, entry: ToolTraceEntry) -> str | None:
        """End with no insights as soon as discovery proves there is no data."""
        if entry.tool != "discover_events" or entry.error is not None:
            return None
        try:
            payload = json.loads(entry.result or "")
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(payload, dict) and payload.get("events") == []:
            logger.info("Event discovery returned no data; producing no insights")
            return "[]"
        return None

    def parse_agentic(self, result: ToolLoopResult) -> Any:
        """Fail closed unless the investigation is grounded in event discovery."""
        discovery = next(
            (
                entry
                for entry in result.trace
                if entry.tool == "discover_events" and entry.error is None
            ),
            None,
        )
        if discovery is None:
            logger.warning(
                "Investigation did not complete event discovery; producing no insights"
            )
            return []
        parsed = super().parse_agentic(result)
        successful_sources = {
            tool_result_source_id(entry)
            for entry in result.trace
            if entry.error is None
        }
        validated: list[dict[str, Any]] = []
        try:
            for raw in parsed:
                insight = BehaviorInsight.model_validate(raw)
                cited = set(insight.evidence.source_ids)
                unknown = sorted(cited - successful_sources)
                if unknown:
                    raise ValueError(
                        "behavior insight cites unknown warehouse source IDs: "
                        + ", ".join(unknown)
                    )
                validated.append(insight.model_dump())
        except (ValidationError, ValueError) as exc:
            logger.error("Behavior insight output failed strict grounding: %s", exc)
            raise ValueError(f"invalid grounded behavior insight: {exc}") from exc
        return validated

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
