"""Experiment design agent.

Consumes behaviour-analysis insights, designs an A/B experiment, runs it
through the safety validator (plus a nuanced LLM safety review), and — gated by
autonomy level — deploys it as a feature flag + experiment config or routes it
to human approval.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.framework import (
    AgentContext,
    BaseAgent,
    GateDecision,
    gate_action,
    register_agent,
)
from app.llm.prompts.experiment import (
    EXPERIMENT_DESIGN_PROMPT,
    EXPERIMENT_DESIGN_SYSTEM,
    SAFETY_REVIEW_PROMPT,
)
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json
from app.safety.validator import ActionType, AgentAction, SafetyValidator
from app.tools.experiments import create_experiment_config, get_active_experiments
from app.tools.flags import create_flag

logger = logging.getLogger(__name__)
_safety = SafetyValidator()


@register_agent
class ExperimentDesignAgent(BaseAgent):
    """Designs, validates, and (autonomy permitting) deploys experiments."""

    name = "experiment_design"
    description = "Design an A/B experiment from insights and deploy it safely."
    order = 20
    system_prompt = EXPERIMENT_DESIGN_SYSTEM
    model_tier = "reasoning"
    memory_query = "experiment results A/B test outcomes"
    memory_top_k = 5
    requires = ("insights",)
    produces = "experiment_designs"
    parse_as = "object"

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            active = await get_active_experiments(project_id=ctx.project_id)
        except Exception as exc:
            logger.warning("Could not fetch active experiments: %s", exc)
            active = []
        return {"active_experiments": active if isinstance(active, list) else []}

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        insight = self._pick_insight(state.get("insights", []))
        if insight is None:
            return None
        return EXPERIMENT_DESIGN_PROMPT.format(
            insight=json.dumps(insight, default=str),
            context=working.get("context", ""),
            active_experiments=json.dumps(working.get("active_experiments", []), default=str),
            baseline_metrics="(to be determined from query data)",
        )

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        if not output:
            return {"deployed": False, "experiment_id": ""}

        safety = await self._safety_check(ctx, output, working.get("active_experiments", []))
        decision = gate_action(ctx.autonomy_level, safety)
        meta: dict[str, Any] = {
            "experiment_id": output.get("experiment_id", ""),
            "safety_result": safety,
            "decision": decision.value,
            "deployed": False,
            "needs_approval": decision is GateDecision.approve,
        }

        if decision is GateDecision.deploy:
            meta["deployed"] = await self._deploy(ctx, output)
        return meta

    def finalize(self, output: Any, action: dict[str, Any]) -> Any:
        # Store as a list so the supervisor's experiment count is uniform.
        return [output] if output else []

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_insight(insights: list[dict]) -> dict | None:
        if not insights:
            return None
        experiment_insights = [
            i
            for i in insights
            if i.get("action_type") == "experiment"
            or i.get("recommended_action", "").lower().startswith("experiment")
        ]
        return (experiment_insights or insights)[0]

    async def _safety_check(
        self, ctx: AgentContext, experiment: dict, active_experiments: list[dict]
    ) -> dict[str, Any]:
        action = AgentAction(
            type=ActionType.create_experiment,
            config=experiment,
            project_id=ctx.project_id,
        )
        result = _safety.validate(action).model_dump()

        # Layer a nuanced LLM safety review on top of the deterministic checks.
        try:
            review_prompt = SAFETY_REVIEW_PROMPT.format(
                experiment=json.dumps(experiment, default=str),
                active_experiments=json.dumps(active_experiments, default=str),
            )
            review = await chat_completion(
                model_tier="fast",
                messages=[
                    {"role": "system", "content": "You are a safety reviewer for A/B experiments."},
                    {"role": "user", "content": review_prompt},
                ],
            )
            parsed = parse_llm_json(review)
            if parsed and not parsed.get("approved", True):
                result["checks"].append({
                    "name": "llm_safety_review",
                    "passed": False,
                    "message": "; ".join(parsed.get("concerns", [])),
                })
                result["passed"] = False
        except Exception as exc:
            logger.warning("LLM safety review failed: %s", exc)

        return result

    async def _deploy(self, ctx: AgentContext, experiment: dict) -> bool:
        try:
            flag_config = experiment.get("flag_config", {})
            flag_variants = flag_config.get("variants", [])
            experiment_variants = experiment.get("variants") or flag_variants
            experiment_id = experiment.get("experiment_id", "")
            flag_key = flag_config.get("key", experiment_id)
            description = experiment.get("description") or experiment.get("hypothesis", "")

            await create_flag(
                project_id=ctx.project_id,
                key=flag_key,
                name=flag_config.get("name") or experiment_id or flag_key,
                description=description,
                state=flag_config.get("state"),
                enabled=flag_config.get("enabled", True),
                default_variant=flag_config.get("default_variant", "control"),
                variants=flag_variants,
                rules=flag_config.get("rules", []),
                fallthrough=flag_config.get("fallthrough"),
                evaluation_mode=flag_config.get("evaluation_mode", "client"),
                auto_disable=flag_config.get("auto_disable", True),
                guardrails=flag_config.get("guardrails", []),
            )
            await create_experiment_config(
                project_id=ctx.project_id,
                experiment_id=experiment_id or flag_key,
                hypothesis=experiment.get("hypothesis", ""),
                variants=experiment_variants,
                primary_metric=experiment.get("primary_metric", {}),
                secondary_metrics=experiment.get("secondary_metrics"),
                guardrail_metrics=experiment.get("guardrail_metrics"),
                targeting=experiment.get("targeting"),
                estimated_duration_days=experiment.get("estimated_duration_days", 14),
                flag_key=flag_key,
            )
            logger.info("Experiment %s deployed successfully", experiment.get("experiment_id"))
            return True
        except Exception as exc:
            logger.error("Failed to deploy experiment: %s", exc)
            return False
