"""Disabled significance-derived feature proposal surface.

Legacy ship verdicts predate the fixed-horizon snapshot contract and are not
eligible evidence. The registered agent remains fail-closed until a separate
human deployment-readiness attestation contract exists.
"""

from __future__ import annotations

import json
from typing import Any

from app.framework import AgentContext, BaseAgent, MemoryEntry, register_agent
from app.llm.prompts.feature import FEATURE_PROPOSAL_SYSTEM


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
    """Registered but disabled legacy experiment-to-proposal path."""

    name = "feature_proposal"
    description = "Unavailable: experiment snapshots do not assess deployment readiness."
    enabled = False
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
        return {"disabled": True, "ship_verdicts": []}

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
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
            "proposals_count": 0,
            "approval_status": "unavailable",
            "needs_approval": False,
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
