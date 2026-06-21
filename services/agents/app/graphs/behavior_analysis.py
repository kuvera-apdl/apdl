"""Behavior analysis agent.

Plans a set of analytics queries with the LLM, runs them against ClickHouse,
synthesises the results into actionable insights, and stores them in long-term
memory for downstream agents. Produces the ``insights`` consumed by the
experiment-design, personalization, and feature-proposal agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.analysis import (
    ANALYSIS_PLAN_PROMPT,
    BEHAVIOR_ANALYSIS_SYSTEM,
    SYNTHESIS_PROMPT,
)
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json
from app.tools.clickhouse import (
    discover_events,
    query_cohort,
    query_events,
    query_funnel,
    query_retention,
    query_timeseries,
)

logger = logging.getLogger(__name__)


def _format_event_catalog(catalog: list[dict[str, Any]]) -> str:
    """Render the discovered event catalog for the planning prompt."""
    if not catalog:
        return (
            "(no events found in this time range — the project may have no data "
            "yet; do not fabricate event names)"
        )
    lines = [
        f"- {entry.get('event_name')}: {entry.get('event_count', 0)} events, "
        f"{entry.get('unique_users', 0)} unique users"
        for entry in catalog
    ]
    return "\n".join(lines)


@register_agent
class BehaviorAnalysisAgent(BaseAgent):
    """Analyses user behaviour and emits prioritised insights."""

    name = "behavior_analysis"
    description = "Plan + run analytics queries and synthesise insights."
    order = 10
    system_prompt = BEHAVIOR_ANALYSIS_SYSTEM
    model_tier = "reasoning"
    memory_query = "recent behavior analysis insights anomalies trends"
    memory_top_k = 5
    produces = "insights"
    parse_as = "list"

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        """Discover real events, plan queries against them, then run the plan."""
        catalog = await self._discover_events(ctx)
        plan = await self._plan(ctx, working.get("context", ""), catalog)
        results = await self._run_queries(ctx, plan.get("queries", []))
        return {
            "event_catalog": catalog,
            "analysis_plan": plan,
            "query_results": results,
        }

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        # No queries ran -> nothing to synthesise -> no insights.
        if not working.get("query_results"):
            return None
        return SYNTHESIS_PROMPT.format(
            query_results=json.dumps(working["query_results"], indent=2, default=str),
            context=working.get("context", ""),
        )

    def parse(self, response: str) -> Any:
        insights = parse_llm_json(
            response,
            [{"title": "Raw analysis", "description": response, "confidence": "low"}],
        )
        return insights if isinstance(insights, list) else [insights]

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

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    async def _plan(
        self, ctx: AgentContext, context: str, event_catalog: list[dict[str, Any]]
    ) -> dict[str, Any]:
        prompt = ANALYSIS_PLAN_PROMPT.format(
            context=context,
            project_id=ctx.project_id,
            time_range_days=ctx.time_range_days,
            event_catalog=_format_event_catalog(event_catalog),
        )
        response = await chat_completion(
            model_tier=self.model_tier,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
        )
        return parse_llm_json(
            response, {"queries": [], "rationale": response, "focus_areas": []}
        )

    async def _discover_events(self, ctx: AgentContext) -> list[dict[str, Any]]:
        """Fetch the real event catalog so the plan targets events that exist."""
        end = date.today()
        start = end - timedelta(days=ctx.time_range_days)
        try:
            result = await discover_events(
                project_id=ctx.project_id,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
            events = result.get("events", [])
            return events if isinstance(events, list) else []
        except Exception as exc:
            logger.warning("event discovery failed: %s", exc)
            return []

    async def _run_queries(
        self, ctx: AgentContext, queries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        end = date.today()
        start = end - timedelta(days=ctx.time_range_days)
        start_str, end_str = start.isoformat(), end.isoformat()
        project_id = ctx.project_id

        def _required(q: dict, field: str) -> Any:
            value = q.get(field)
            if value is None:
                raise ValueError(f"Missing required field '{field}' for {q.get('type', 'unknown')} query")
            return value

        async def _run_one(q: dict) -> dict[str, Any]:
            query_type = q.get("type")
            try:
                if query_type is None:
                    raise ValueError("Missing required field 'type' for query")

                if query_type == "event_count":
                    selectors = q.get("selectors")
                    if not selectors:
                        # Tolerate a loose plan that listed bare event names.
                        names = q.get("event_names") or []
                        selectors = [
                            {"event_name": name, "filters": []} for name in names
                        ]
                    if not selectors:
                        raise ValueError(
                            "Missing required field 'selectors' for event_count query"
                        )
                    result = await query_events(
                        project_id=project_id,
                        start_date=start_str,
                        end_date=end_str,
                        selectors=selectors,
                    )
                elif query_type == "timeseries":
                    result = await query_timeseries(
                        project_id=project_id,
                        selector=_required(q, "selector"),
                        start_date=start_str,
                        end_date=end_str,
                        interval=q.get("interval", "1 DAY"),
                    )
                elif query_type == "funnel":
                    result = await query_funnel(
                        project_id=project_id,
                        steps=_required(q, "steps"),
                        start_date=start_str,
                        end_date=end_str,
                    )
                elif query_type == "retention":
                    result = await query_retention(
                        project_id=project_id,
                        cohort_selector=_required(q, "cohort_selector"),
                        return_selector=_required(q, "return_selector"),
                        start_date=start_str,
                        end_date=end_str,
                        period=q.get("period", "day"),
                    )
                elif query_type == "cohort":
                    result = await query_cohort(
                        project_id=project_id,
                        cohort_property=_required(q, "cohort_property"),
                        metric_selector=_required(q, "metric_selector"),
                        start_date=start_str,
                        end_date=end_str,
                    )
                else:
                    result = {"error": f"Unknown query type: {query_type}"}
                return {"type": query_type, "params": q, "result": result}
            except Exception as exc:
                logger.error("Query failed (%s): %s", query_type, exc)
                return {"type": query_type, "params": q, "error": str(exc)}

        return list(await asyncio.gather(*[_run_one(q) for q in queries]))
