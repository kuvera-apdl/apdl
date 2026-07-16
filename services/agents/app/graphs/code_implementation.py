"""Code implementation agent.

Consumes human-approved feature proposals from the work queue (decision D2) and
turns each into a draft pull request by delegating to the codegen service. The
proposal was already approved, so this agent does the *building*: opening a PR is
gated through the shared autonomy gate (low risk — a draft PR is reversible), and
merging is gated separately with green-CI enforcement (Phase 6). Produces
``changesets``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.framework import (
    AgentContext,
    AgentResult,
    BaseAgent,
    GateDecision,
    gate_action,
    register_agent,
)
from app.safety.validator import ActionType, AgentAction, SafetyValidator
from app.store.proposals import claim_proposals, mark_failed

logger = logging.getLogger(__name__)
_safety = SafetyValidator()


@register_agent
class CodeImplementationAgent(BaseAgent):
    """Implements approved feature proposals as draft PRs via codegen."""

    name = "code_implementation"
    description = "Turn approved feature proposals into draft pull requests."
    order = 50
    memory_query = None
    requires = ()
    produces = "changesets"
    parse_as = "list"
    max_proposals = 5

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        # L1 is suggest-only: never claim or build.
        if ctx.autonomy_level <= 1:
            return {"claimed_proposals": []}
        try:
            # A forked run carries target_proposal_id and claims exactly that
            # proposal (one PR per approval); an unscoped run drains the queue.
            claimed = await claim_proposals(
                ctx.pool,
                ctx.project_id,
                ctx.run_id,
                self.max_proposals,
                proposal_id=ctx.target_proposal_id,
            )
        except Exception as exc:
            logger.warning("Could not claim feature proposals: %s", exc)
            claimed = []
        return {"claimed_proposals": claimed}

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        # No reasoning step — the proposal is the spec; implementation is
        # delegated to codegen. Skipping the prompt skips the LLM call.
        return None

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        claimed = working.get("claimed_proposals", [])
        changesets = [await self._implement_one(ctx, p) for p in claimed]
        return {
            "changesets": changesets,
            "opened": sum(1 for c in changesets if c.get("changeset_id")),
            "needs_approval": any(c.get("decision") == "approve" for c in changesets),
        }

    def finalize(self, output: Any, action: dict[str, Any]) -> Any:
        return action.get("changesets", [])

    # ------------------------------------------------------------------

    async def _implement_one(self, ctx: AgentContext, proposal: dict[str, Any]) -> dict[str, Any]:
        proposal_id = proposal.get("proposal_id", "")
        title = proposal.get("title", "")
        spec = proposal.get("spec", "")

        safety = _safety.validate(
            AgentAction(
                type=ActionType.open_pull_request,
                config={"title": title, "spec": spec},
                project_id=ctx.project_id,
            )
        ).model_dump()
        decision = gate_action(
            ctx.autonomy_level,
            safety,
            # A draft pull request is still an external repository mutation.
            # The durable approval worker is the only path allowed to enqueue
            # it; agent reasoning never calls Codegen directly.
            always_require_approval=True,
        )
        result: dict[str, Any] = {
            "proposal_id": proposal_id,
            # Carry the spec on the gate item so the approval handler can open
            # the PR straight from the persisted changeset (Phase 6) without a
            # re-read, and the console can show what is being approved.
            "title": title,
            "spec": spec,
            "decision": decision.value,
            "safety_result": safety,
        }

        # ``always_require_approval`` makes deploy unreachable by contract.
        # The persisted gate item is the only plan; the approval outbox worker
        # is the sole caller of Codegen.
        if decision is GateDecision.deploy:
            raise RuntimeError("code implementation bypassed its mandatory approval gate")
        return result

    async def after_result_persisted(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        result: AgentResult,
    ) -> None:
        """Terminalize safety-halted claims only after their result is durable."""
        await super().after_result_persisted(ctx, state, result)
        if not isinstance(result.output, list):
            return
        for changeset in result.output:
            if not isinstance(changeset, dict):
                continue
            if changeset.get("decision") != GateDecision.halt.value:
                continue
            safety = changeset.get("safety_result")
            if isinstance(safety, dict) and safety.get("passed") is False:
                await self._safe_fail(
                    ctx,
                    str(changeset.get("proposal_id") or ""),
                    "Failed safety validation.",
                )

    @staticmethod
    async def _safe_fail(ctx: AgentContext, proposal_id: str, error: str) -> None:
        try:
            await mark_failed(
                ctx.pool, ctx.project_id, proposal_id, error, ctx.run_id
            )
        except Exception as exc:
            logger.warning("Could not mark proposal %s failed: %s", proposal_id, exc)
