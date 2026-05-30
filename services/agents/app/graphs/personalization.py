"""Personalization agent — graph-based workflow.

Analyses user segments from behavior insights and generates server-driven
UI configurations to personalise the user experience.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import TypedDict

from app.graphs.runner import END, Graph
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json
from app.llm.prompts.personalize import PERSONALIZATION_PROMPT, PERSONALIZATION_SYSTEM
from app.memory.pgvector_store import PgVectorStore
from app.safety.validator import AgentAction, ActionType, SafetyValidator
from app.tools.clickhouse import query_breakdown
from app.tools.ui_config import create_ui_config, list_ui_configs

logger = logging.getLogger(__name__)
safety_validator = SafetyValidator()


class PersonalizationState(TypedDict, total=False):
    """State passed between nodes in the personalization graph."""
    project_id: str
    autonomy_level: int
    insights: list[dict]
    segments: list[dict]
    context: str
    ui_configs: list[dict]       # generated configurations
    existing_configs: list[dict]  # already deployed configs
    deployed_count: int
    error: str | None


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------

async def gather_segments(state: PersonalizationState) -> PersonalizationState:
    """Gather user segment data from event breakdowns."""
    project_id = state["project_id"]
    segments: list[dict] = []

    # Extract segment dimensions from insights
    properties_to_check = {"plan", "platform", "country", "source", "device_type"}
    end = date.today()
    start = end - timedelta(days=7)
    start_str, end_str = start.isoformat(), end.isoformat()

    async def _fetch_breakdown(prop: str) -> dict | None:
        try:
            result = await query_breakdown(
                project_id=project_id,
                selector={"event_name": "page_view", "filters": []},
                property_name=prop,
                start_date=start_str,
                end_date=end_str,
                limit=10,
            )
            if isinstance(result, dict) and result.get("results"):
                return {"property": prop, "breakdown": result["results"]}
        except Exception as exc:
            logger.debug("Segment breakdown for %s failed: %s", prop, exc)
        return None

    breakdown_results = await asyncio.gather(*[_fetch_breakdown(p) for p in properties_to_check])
    segments = [r for r in breakdown_results if r is not None]

    # Also fetch existing UI configs to avoid duplication
    try:
        existing = await list_ui_configs(project_id=project_id)
        state["existing_configs"] = existing if isinstance(existing, list) else []
    except Exception:
        state["existing_configs"] = []

    state["segments"] = segments
    return state


async def retrieve_context(state: PersonalizationState) -> PersonalizationState:
    """Retrieve historical personalization context from memory."""
    vector_store: PgVectorStore | None = state.get("_vector_store")  # type: ignore[assignment]
    context_parts: list[str] = []

    if vector_store:
        try:
            memories = await vector_store.search(
                project_id=state["project_id"],
                query="personalization UI configuration segments",
                top_k=3,
            )
            context_parts = [m["content"] for m in memories]
        except Exception:
            pass

    state["context"] = "\n---\n".join(context_parts) if context_parts else ""
    return state


async def generate_configs(state: PersonalizationState) -> PersonalizationState:
    """Use fast LLM to generate UI personalization configurations."""
    insights = state.get("insights", [])
    segments = state.get("segments", [])
    context = state.get("context", "")

    prompt = PERSONALIZATION_PROMPT.format(
        insights=json.dumps(insights, default=str),
        segments=json.dumps(segments, default=str),
        context=context,
    )

    response = await chat_completion(
        model_tier="fast",
        messages=[
            {"role": "system", "content": PERSONALIZATION_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    configs = parse_llm_json(response, [])
    if not isinstance(configs, list):
        configs = [configs] if configs else []

    state["ui_configs"] = configs
    return state


async def deploy_configs(state: PersonalizationState) -> PersonalizationState:
    """Validate and deploy UI configurations via the config service."""
    configs = state.get("ui_configs", [])
    project_id = state["project_id"]
    autonomy = state.get("autonomy_level", 2)
    deployed = 0

    for config in configs:
        # Safety check
        action = AgentAction(
            type=ActionType.update_ui_config,
            config=config,
            project_id=project_id,
        )
        safety_result = safety_validator.validate(action)

        if not safety_result.passed:
            logger.warning(
                "UI config %s failed safety: %s",
                config.get("config_id", "unknown"),
                [c["message"] for c in safety_result.checks if not c["passed"]],
            )
            continue

        # At L1 autonomy, only suggest — don't deploy
        if autonomy < 2:
            logger.info("L1 autonomy: skipping deployment of %s", config.get("config_id"))
            continue

        try:
            await create_ui_config(
                project_id=project_id,
                config_id=config.get("config_id", f"ui_auto_{deployed}"),
                component=config.get("component", "feature_card"),
                targeting=config.get("targeting", {}),
                layout=config.get("layout", {"type": "default", "children": []}),
                content=config.get("content", {}),
                priority=config.get("priority", 10),
            )
            deployed += 1
        except Exception as exc:
            logger.error("Failed to deploy UI config: %s", exc)

    state["deployed_count"] = deployed
    return state


async def store_results(state: PersonalizationState) -> PersonalizationState:
    """Store personalization results in vector memory."""
    vector_store: PgVectorStore | None = state.get("_vector_store")  # type: ignore[assignment]
    configs = state.get("ui_configs", [])

    if vector_store and configs:
        summary = json.dumps({
            "generated_configs": len(configs),
            "deployed": state.get("deployed_count", 0),
            "config_ids": [c.get("config_id", "") for c in configs],
        })
        try:
            await vector_store.store(
                project_id=state["project_id"],
                content=summary,
                metadata={"type": "personalization_result"},
            )
        except Exception as exc:
            logger.error("Failed to store personalization result: %s", exc)

    return state


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------

def build_personalization_graph() -> Graph:
    """Build the personalization graph."""
    graph = Graph()

    graph.add_node("gather_segments", gather_segments)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("generate_configs", generate_configs)
    graph.add_node("deploy_configs", deploy_configs)
    graph.add_node("store_results", store_results)

    graph.set_entry_point("gather_segments")
    graph.add_edge("gather_segments", "retrieve_context")
    graph.add_edge("retrieve_context", "generate_configs")
    graph.add_edge("generate_configs", "deploy_configs")
    graph.add_edge("deploy_configs", "store_results")
    graph.add_edge("store_results", END)

    return graph


personalization_graph = build_personalization_graph()
