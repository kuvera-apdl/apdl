"""Behavior analysis agent — graph-based workflow.

Analyses user behavior data via ClickHouse queries, synthesises insights,
and stores them in the vector memory for downstream agents.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any, TypedDict

from app.graphs.runner import END, Graph
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json
from app.llm.prompts.analysis import (
    ANALYSIS_PLAN_PROMPT,
    BEHAVIOR_ANALYSIS_SYSTEM,
    SYNTHESIS_PROMPT,
)
from app.memory.pgvector_store import PgVectorStore
from app.tools.clickhouse import (
    query_cohort,
    query_events,
    query_funnel,
    query_retention,
    query_timeseries,
)

logger = logging.getLogger(__name__)


class AnalysisState(TypedDict, total=False):
    """State passed between nodes in the behavior analysis graph."""
    project_id: str
    time_range_days: int
    context: str               # retrieved historical context
    analysis_plan: dict        # LLM-generated plan
    query_results: list[dict]  # results from ClickHouse queries
    insights: list[dict]       # synthesised insights
    error: str | None


# --------------------------------------------------------------------------
# Node implementations
# --------------------------------------------------------------------------

async def retrieve_context(state: AnalysisState) -> AnalysisState:
    """Retrieve relevant historical context from vector memory."""
    vector_store: PgVectorStore = state.get("_vector_store")  # type: ignore[assignment]
    project_id = state["project_id"]

    context_parts: list[str] = []
    if vector_store:
        try:
            memories = await vector_store.search(
                project_id=project_id,
                query="recent behavior analysis insights anomalies trends",
                top_k=5,
            )
            for mem in memories:
                context_parts.append(mem["content"])
        except Exception as exc:
            logger.warning("Failed to retrieve context: %s", exc)

    state["context"] = "\n---\n".join(context_parts) if context_parts else "No previous context available."
    return state


async def plan_analysis(state: AnalysisState) -> AnalysisState:
    """Use reasoning LLM to create an analysis plan."""
    prompt = ANALYSIS_PLAN_PROMPT.format(
        context=state.get("context", ""),
        project_id=state["project_id"],
        time_range_days=state.get("time_range_days", 7),
    )

    response = await chat_completion(
        model_tier="reasoning",
        messages=[
            {"role": "system", "content": BEHAVIOR_ANALYSIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    plan = parse_llm_json(response, {"queries": [], "rationale": response, "focus_areas": []})

    state["analysis_plan"] = plan
    return state


async def run_queries(state: AnalysisState) -> AnalysisState:
    """Execute the queries specified in the analysis plan."""
    plan = state.get("analysis_plan", {})
    queries = plan.get("queries", [])
    project_id = state["project_id"]
    days = state.get("time_range_days", 7)
    end = date.today()
    start = end - timedelta(days=days)
    start_str = start.isoformat()
    end_str = end.isoformat()

    async def _run_one(q: dict) -> dict[str, Any]:
        query_type = q.get("type", "event_count")
        try:
            if query_type == "event_count":
                result = await query_events(
                    project_id=project_id,
                    start_date=start_str,
                    end_date=end_str,
                    event_names=q.get("event_names"),
                )
            elif query_type == "timeseries":
                result = await query_timeseries(
                    project_id=project_id,
                    event_name=q.get("event_name", "page_view"),
                    start_date=start_str,
                    end_date=end_str,
                    interval=q.get("interval", "1 DAY"),
                )
            elif query_type == "funnel":
                result = await query_funnel(
                    project_id=project_id,
                    steps=q.get("steps", []),
                    start_date=start_str,
                    end_date=end_str,
                )
            elif query_type == "retention":
                result = await query_retention(
                    project_id=project_id,
                    cohort_event=q.get("cohort_event", "signup"),
                    return_event=q.get("return_event", "page_view"),
                    start_date=start_str,
                    end_date=end_str,
                    period=q.get("period", "day"),
                )
            elif query_type == "cohort":
                result = await query_cohort(
                    project_id=project_id,
                    cohort_property=q.get("cohort_property", "plan"),
                    metric_event=q.get("metric_event", "page_view"),
                    start_date=start_str,
                    end_date=end_str,
                )
            else:
                result = {"error": f"Unknown query type: {query_type}"}
            return {"type": query_type, "params": q, "result": result}
        except Exception as exc:
            logger.error("Query failed (%s): %s", query_type, exc)
            return {"type": query_type, "params": q, "error": str(exc)}

    state["query_results"] = list(await asyncio.gather(*[_run_one(q) for q in queries]))
    return state


async def synthesize_insights(state: AnalysisState) -> AnalysisState:
    """Use reasoning LLM to synthesise query results into actionable insights."""
    query_results_str = json.dumps(state.get("query_results", []), indent=2, default=str)
    context = state.get("context", "")

    prompt = SYNTHESIS_PROMPT.format(
        query_results=query_results_str,
        context=context,
    )

    response = await chat_completion(
        model_tier="reasoning",
        messages=[
            {"role": "system", "content": BEHAVIOR_ANALYSIS_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    insights = parse_llm_json(
        response, [{"title": "Raw analysis", "description": response, "confidence": "low"}]
    )
    if not isinstance(insights, list):
        insights = [insights]

    state["insights"] = insights
    return state


async def store_insights(state: AnalysisState) -> AnalysisState:
    """Persist insights to vector memory for future retrieval."""
    vector_store: PgVectorStore | None = state.get("_vector_store")  # type: ignore[assignment]
    project_id = state["project_id"]
    insights = state.get("insights", [])

    if vector_store and insights:
        for insight in insights:
            content = json.dumps(insight, default=str)
            try:
                await vector_store.store(
                    project_id=project_id,
                    content=content,
                    metadata={
                        "type": "behavior_insight",
                        "title": insight.get("title", ""),
                        "confidence": insight.get("confidence", "unknown"),
                    },
                )
            except Exception as exc:
                logger.error("Failed to store insight: %s", exc)

    return state


# --------------------------------------------------------------------------
# Conditional edge: should we continue or stop?
# --------------------------------------------------------------------------

def should_continue(state: AnalysisState) -> str:
    """Route after plan_analysis — continue if we have queries to run."""
    plan = state.get("analysis_plan", {})
    queries = plan.get("queries", [])
    if queries:
        return "run_queries"
    return "end"


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------

def build_behavior_analysis_graph() -> Graph:
    """Build the behavior analysis graph."""
    graph = Graph()

    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("plan_analysis", plan_analysis)
    graph.add_node("run_queries", run_queries)
    graph.add_node("synthesize_insights", synthesize_insights)
    graph.add_node("store_insights", store_insights)

    graph.set_entry_point("retrieve_context")
    graph.add_edge("retrieve_context", "plan_analysis")

    graph.add_conditional_edges(
        "plan_analysis",
        should_continue,
        {"run_queries": "run_queries", "end": END},
    )

    graph.add_edge("run_queries", "synthesize_insights")
    graph.add_edge("synthesize_insights", "store_insights")
    graph.add_edge("store_insights", END)

    return graph


behavior_analysis_graph = build_behavior_analysis_graph()
