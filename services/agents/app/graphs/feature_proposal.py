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
from app.store.proposals import list_recent_proposals
from app.tools.code import get_repo_context, list_changesets
from app.tools.experiments import get_active_experiments, get_experiment_results

logger = logging.getLogger(__name__)


def _render_repo_capabilities(context: dict[str, Any]) -> str:
    """Render codegen's repo-context document for the proposal prompt.

    An empty context (codegen down, no connection) degrades to an explicit
    warning rather than a silent blank, so the LLM knows it is proposing blind
    and should stay conservative.
    """
    if not context:
        return (
            "(repository context unavailable — propose only small, conservative "
            "changes that any web application could implement)"
        )
    lines = [
        f"Repository: {context.get('repo', '?')} (branch {context.get('branch', '?')})",
        f"Stack: {context.get('framework', 'unknown')}",
        f"Test script present: {'yes' if context.get('has_test_script') else 'no'}",
    ]
    scripts = context.get("scripts") or {}
    if scripts:
        lines.append("package.json scripts: " + ", ".join(sorted(scripts)))
    readme = str(context.get("readme_excerpt") or "").strip()
    if readme:
        lines.append(f"README (excerpt):\n{readme}")
    paths = context.get("paths") or []
    if paths:
        listing = "\n".join(paths)
        if context.get("paths_truncated"):
            listing += "\n(file list truncated)"
        lines.append(f"Files:\n{listing}")
    return "\n".join(lines)


def _render_existing_work(
    proposals: list[dict[str, Any]], changesets: list[dict[str, Any]]
) -> str:
    """One line per prior proposal / changeset, deduped by title, for the prompt."""
    lines: list[str] = []
    seen: set[str] = set()

    def _add(title: str, detail: str) -> None:
        key = title.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        lines.append(f"- {title.strip()} ({detail})")

    for proposal in proposals:
        _add(str(proposal.get("title") or ""), f"proposal, {proposal.get('status', '?')}")
    for changeset in changesets:
        task = changeset.get("task") or {}
        detail = f"changeset, {changeset.get('status', '?')}"
        if changeset.get("pr_url"):
            detail += f", {changeset['pr_url']}"
        _add(str(task.get("title") or ""), detail)
    return "\n".join(lines) if lines else "(none)"


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
    # A premise-checking budget: verify the events a proposal's success
    # criteria depend on actually fire (and at what magnitude) before the
    # proposal becomes a work order for the coding agent.
    agentic_tools = ("discover_events", "query_events", "query_funnel", "query_breakdown")
    max_tool_steps = 4

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

        # Repo grounding: what the connected repository actually is. Proposals
        # written blind demand infrastructure the repo does not have; the coding
        # agent downstream then fabricates or descopes (the observed junk-PR
        # failure modes). Fetch failures degrade to an explicit "unavailable".
        repo_context: dict[str, Any] = {}
        try:
            fetched_context = await get_repo_context(ctx.project_id)
            if isinstance(fetched_context, dict):
                repo_context = fetched_context
        except Exception as exc:
            logger.warning("Could not fetch repo context: %s", exc)

        # Dedup grounding: what was already proposed or is already in flight.
        # Insights barely change between runs, so without this every run
        # re-proposes the same themes and each becomes a duplicate PR.
        recent_proposals: list[dict[str, Any]] = []
        try:
            recent_proposals = await list_recent_proposals(ctx.pool, ctx.project_id)
        except Exception as exc:
            logger.warning("Could not list recent proposals: %s", exc)
        changesets: list[dict[str, Any]] = []
        try:
            listed = await list_changesets(ctx.project_id)
            if isinstance(listed, list):
                changesets = listed
        except Exception as exc:
            logger.warning("Could not list changesets: %s", exc)

        return {
            "active_experiments": active,
            "experiment_results": [r for r in fetched if r is not None],
            "repo_context": repo_context,
            "recent_proposals": recent_proposals,
            "changesets": changesets,
        }

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        return FEATURE_PROPOSAL_PROMPT.format(
            experiment_results=json.dumps(working.get("experiment_results", []), default=str),
            insights=json.dumps(state.get("insights", []), default=str),
            context=working.get("context", ""),
            capabilities=_render_repo_capabilities(working.get("repo_context") or {}),
            existing_work=_render_existing_work(
                working.get("recent_proposals") or [],
                working.get("changesets") or [],
            ),
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
        # Un-reviewed proposals are never persisted: the approvals endpoint
        # stores each proposal to vector memory at the moment a human approves
        # it (see routers/approvals.py), so memory only carries proposals that
        # cleared the gate.
        return []
