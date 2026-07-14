"""Feature proposal agent — durable features from winning experiments.

Loop-closure phase 4: proposals are no longer invented from insights. The
agent drains unconsumed ``ship`` verdicts (the evaluation agent's durable
work queue) and writes one flag-removal work order per validated win — make
the treatment permanent, delete the control path. No winning experiment, no
proposal; the LLM is not even called.

Proposals ALWAYS require human approval — regardless of autonomy level —
because of their product and engineering blast radius. Produces
``feature_proposals``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.feature import FEATURE_PROPOSAL_PROMPT, FEATURE_PROPOSAL_SYSTEM
from app.store.proposals import list_recent_proposals
from app.store.verdicts import list_unconsumed_ship_verdicts, mark_verdicts_consumed
from app.tools.code import get_repo_context, list_changesets

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
        "Languages: " + (", ".join(context.get("languages") or []) or "unknown"),
        "Frameworks: "
        + (", ".join(context.get("frameworks") or []) or "none detected"),
    ]
    commands = context.get("commands") or []
    if commands:
        lines.append(
            "Commands:\n"
            + "\n".join(
                f"- {command.get('kind', '?')}: {command.get('command', '?')} "
                f"(cwd {command.get('cwd', '.')})"
                for command in commands
            )
        )
    facilities = context.get("test_facilities") or []
    lines.append(
        "Test facilities: "
        + (", ".join(item.get("name", "?") for item in facilities) or "none detected")
    )
    for label, key in (
        ("Packages", "packages"),
        ("Routes", "routes"),
        ("Entrypoints", "entrypoints"),
        ("Services", "services"),
        ("Deployments", "deployment_targets"),
        ("CI workflows", "ci_workflows"),
    ):
        values = context.get(key) or []
        if values:
            lines.append(
                f"{label}: " + ", ".join(str(item.get("path", "?")) for item in values)
            )
    instructions = context.get("instructions") or []
    if instructions:
        lines.append(
            "Repository instructions:\n"
            + "\n\n".join(
                f"[{item.get('path', '?')} scoped to {item.get('scope', '.')}]:\n"
                f"{str(item.get('content', ''))}"
                for item in instructions
            )
        )
    protection = context.get("branch_protection") or {}
    lines.append(f"Branch protection: {protection.get('status', 'unknown')}")
    uncertainties = context.get("uncertainties") or []
    if uncertainties:
        lines.append(
            "Uncertainties:\n"
            + "\n".join(
                f"- {item.get('code', '?')}: {item.get('message', '')}"
                for item in uncertainties
            )
        )
    paths = context.get("paths") or []
    if paths:
        listing = "\n".join(paths[:400])
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


def _render_ship_verdicts(verdicts: list[dict[str, Any]]) -> str:
    """The winning experiments, with the evaluator's numbers and notes."""
    rendered = [
        {
            "experiment_id": v.get("experiment_id", ""),
            "reasoning": v.get("reasoning", ""),
            "results": v.get("results", {}),
            "durable_feature": v.get("durable_feature", ""),
        }
        for v in verdicts
    ]
    return json.dumps(rendered, default=str)


@register_agent
class FeatureProposalAgent(BaseAgent):
    """Turns winning experiments into durable-feature proposals; always routes
    to human approval."""

    name = "feature_proposal"
    description = "Write durable-feature work orders from winning experiments."
    order = 40
    system_prompt = FEATURE_PROPOSAL_SYSTEM
    model_tier = "reasoning"
    memory_query = "approved feature proposals experiment wins durable features"
    memory_top_k = 5
    # No state dependency: the ship-verdict queue is durable, so this agent is
    # as runnable in a scheduled evaluation pipeline as after a fresh analysis.
    requires = ()
    produces = "feature_proposals"
    parse_as = "list"
    # A small verification budget for instrumentation details (does the win's
    # metric event still fire) — the win itself needs no re-verification.
    agentic_tools = ("discover_events", "query_events")
    max_tool_steps = 2
    max_wins_per_run = 5

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        # The durable ship-verdict queue is the ONLY proposal source: no
        # winning experiment, no proposal (the product thesis made structural).
        ship_verdicts: list[dict[str, Any]] = []
        if ctx.pool is not None:
            try:
                ship_verdicts = await list_unconsumed_ship_verdicts(
                    ctx.pool, ctx.project_id, self.max_wins_per_run
                )
            except Exception as exc:
                logger.warning("Could not list ship verdicts: %s", exc)
        if not ship_verdicts:
            return {"ship_verdicts": []}

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
            "ship_verdicts": ship_verdicts,
            "repo_context": repo_context,
            "recent_proposals": recent_proposals,
            "changesets": changesets,
        }

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        ship_verdicts = working.get("ship_verdicts", [])
        if not ship_verdicts:
            # No winning experiments → no proposals, no LLM call.
            return None
        return FEATURE_PROPOSAL_PROMPT.format(
            ship_verdicts=_render_ship_verdicts(ship_verdicts),
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
        # Consume the verdicts this run turned into proposals, so reruns never
        # propose the same win twice. A win whose proposal the model dropped
        # (e.g. already in flight) is consumed too — the existing-work list
        # said it's covered, and re-offering it every run would spam the gate.
        consumed_ids = [
            v["id"]
            for v in working.get("ship_verdicts", [])
            if isinstance(v.get("id"), int)
        ]
        if consumed_ids and ctx.pool is not None:
            try:
                await mark_verdicts_consumed(ctx.pool, consumed_ids)
            except Exception as exc:
                logger.warning("Could not mark verdicts consumed: %s", exc)

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
