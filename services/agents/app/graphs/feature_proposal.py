"""Feature proposal agent.

Analyses experiment results and behaviour insights to propose concrete new
features with implementation specs. Proposals ALWAYS require human approval —
regardless of autonomy level — because of their product and engineering blast
radius. Produces ``feature_proposals``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.feature import FEATURE_PROPOSAL_PROMPT, FEATURE_PROPOSAL_SYSTEM
from app.tools.experiments import get_active_experiments, get_experiment_results

logger = logging.getLogger(__name__)


@register_agent
class FeatureProposalAgent(BaseAgent):
    """Proposes new features; always routes to human approval."""

    name = "feature_proposal"
    description = "Propose new features from experiment results and insights."
    order = 40
    system_prompt = FEATURE_PROPOSAL_SYSTEM
    model_tier = "reasoning"
    memory_query = "feature proposals product capabilities experiment results"
    memory_top_k = 5
    requires = ("insights",)
    produces = "feature_proposals"
    parse_as = "list"

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            active = await get_active_experiments(project_id=ctx.project_id)
            active = active if isinstance(active, list) else []
        except Exception as exc:
            logger.warning("Could not fetch active experiments: %s", exc)
            active = []

        async def _fetch_result(exp: dict) -> dict[str, Any] | None:
            if not isinstance(exp, dict):
                return None
            exp_id = exp.get("experiment_id", "")
            metric = (exp.get("primary_metric") or {}).get("event", "")
            if not exp_id or not metric:
                return None
            try:
                return await get_experiment_results(
                    experiment_id=exp_id,
                    metric=metric,
                    project_id=ctx.project_id,
                    flag_key=exp.get("flag_key") or exp_id,
                )
            except Exception as exc:
                logger.debug("Could not fetch results for %s: %s", exp_id, exc)
                return None

        fetched = await asyncio.gather(*[_fetch_result(e) for e in active])
        return {
            "active_experiments": active,
            "experiment_results": [r for r in fetched if r is not None],
        }

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        return FEATURE_PROPOSAL_PROMPT.format(
            experiment_results=json.dumps(working.get("experiment_results", []), default=str),
            insights=json.dumps(state.get("insights", []), default=str),
            context=working.get("context", ""),
            capabilities="(determined from project configuration)",
        )

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        # Feature proposals never auto-deploy; they wait for human approval.
        return {
            "proposals_count": len(output),
            "approval_status": "pending" if output else "none",
            "needs_approval": bool(output),
        }

    def memory_entries(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
        action: dict[str, Any],
    ) -> list[MemoryEntry]:
        # Proposals are persisted only once a human approves them (set by the
        # approvals endpoint, which re-runs storage with state["approved"]).
        if not state.get("approved"):
            return []
        return [
            MemoryEntry(
                content=json.dumps(proposal, default=str),
                metadata={
                    "type": "feature_proposal",
                    "proposal_id": proposal.get("proposal_id", ""),
                    "priority": proposal.get("priority", "P2"),
                    "status": "approved",
                },
            )
            for proposal in output
        ]
