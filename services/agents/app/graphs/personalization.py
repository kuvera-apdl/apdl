"""Personalization agent.

Builds user-segment context from event breakdowns, generates server-driven UI
configurations for those segments, safety-validates each one, and deploys them
when autonomy allows. Produces ``personalizations``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.personalize import PERSONALIZATION_PROMPT, PERSONALIZATION_SYSTEM
from app.safety.validator import ActionType, AgentAction, SafetyValidator
from app.tools.clickhouse import query_breakdown
from app.tools.ui_config import create_ui_config, list_ui_configs

logger = logging.getLogger(__name__)
_safety = SafetyValidator()

_SEGMENT_PROPERTIES = ("plan", "platform", "country", "source", "device_type")


@register_agent
class PersonalizationAgent(BaseAgent):
    """Generates and deploys segment-targeted UI configurations."""

    name = "personalization"
    description = "Generate server-driven UI configs for user segments."
    # Parked: the config service has no /v1/admin/ui-configs endpoints, so
    # every deploy 404s and nothing downstream consumes the output. Stays
    # registered so 'personalizations' remains a valid produces key, but is
    # hidden from listings and skipped by the supervisor until the delivery
    # path (config storage + SSE/envelope + SDK) exists.
    enabled = False
    order = 30
    system_prompt = PERSONALIZATION_SYSTEM
    model_tier = "fast"
    memory_query = "personalization UI configuration segments"
    memory_top_k = 3
    requires = ("insights",)
    produces = "personalizations"
    parse_as = "list"

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        end = date.today()
        start = end - timedelta(days=ctx.time_range_days)
        start_str, end_str = start.isoformat(), end.isoformat()

        async def _fetch_breakdown(prop: str) -> dict | None:
            try:
                result = await query_breakdown(
                    project_id=ctx.project_id,
                    # Real ingested data names pageviews "page" (the JS SDK's
                    # name, standardized project-wide) — "page_view" matched
                    # nothing, so every breakdown came back empty and the
                    # personalization prompt ran blind.
                    selector={"event_name": "page", "filters": []},
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

        breakdowns = await asyncio.gather(
            *[_fetch_breakdown(p) for p in _SEGMENT_PROPERTIES]
        )
        try:
            existing = await list_ui_configs(project_id=ctx.project_id)
        except Exception:
            existing = []

        return {
            "segments": [b for b in breakdowns if b is not None],
            "existing_configs": existing if isinstance(existing, list) else [],
        }

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        return PERSONALIZATION_PROMPT.format(
            insights=json.dumps(state.get("insights", []), default=str),
            segments=json.dumps(working.get("segments", []), default=str),
            context=working.get("context", ""),
        )

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        deployed = 0
        for idx, config in enumerate(output):
            action = AgentAction(
                type=ActionType.update_ui_config,
                config=config,
                project_id=ctx.project_id,
            )
            safety = _safety.validate(action)
            if not safety.passed:
                logger.warning(
                    "UI config %s failed safety: %s",
                    config.get("config_id", "unknown"),
                    [c["message"] for c in safety.checks if not c["passed"]],
                )
                continue

            # L1 autonomy is suggest-only.
            if ctx.autonomy_level < 2:
                continue

            try:
                await create_ui_config(
                    project_id=ctx.project_id,
                    config_id=config.get("config_id", f"ui_auto_{idx}"),
                    component=config.get("component", "feature_card"),
                    targeting=config.get("targeting", {}),
                    layout=config.get("layout", {"type": "default", "children": []}),
                    content=config.get("content", {}),
                    priority=config.get("priority", 10),
                )
                deployed += 1
            except Exception as exc:
                logger.error("Failed to deploy UI config: %s", exc)

        return {"deployed_count": deployed, "configs_generated": len(output)}

    def memory_entries(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
        action: dict[str, Any],
    ) -> list[MemoryEntry]:
        if not output:
            return []
        summary = json.dumps({
            "generated_configs": len(output),
            "deployed": action.get("deployed_count", 0),
            "config_ids": [c.get("config_id", "") for c in output],
        })
        return [MemoryEntry(content=summary, metadata={"type": "personalization_result"})]
