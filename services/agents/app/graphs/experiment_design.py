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
from app.safety.validator import (
    ActionType,
    AgentAction,
    SafetyValidator,
    _max_rollout_percentage,
)
from app.tools.experiments import create_experiment_config, get_active_experiments

logger = logging.getLogger(__name__)
_safety = SafetyValidator()

_CANONICAL_VARIANT_FIELDS = ("key", "weight")


def _is_canonical_rule(rule: Any) -> bool:
    """True for a flag rule the safety validator accepts: a valid rollout and no
    variant-assignment fields."""
    if not isinstance(rule, dict):
        return False
    if "variants" in rule or "default_variant" in rule:
        return False
    rollout = rule.get("rollout")
    if not isinstance(rollout, dict) or set(rollout) - {"percentage", "bucket_by"}:
        return False
    percentage = rollout.get("percentage")
    return (
        isinstance(percentage, int | float)
        and not isinstance(percentage, bool)
        and 0 <= percentage <= 100
        and isinstance(rollout.get("bucket_by"), str)
        and bool(rollout.get("bucket_by"))
    )


def _canonicalize_flag_config(experiment: dict[str, Any]) -> None:
    """Coerce an LLM-authored ``flag_config`` into the canonical shape the safety
    validator enforces, mutating ``experiment`` in place.

    Variants keep only ``key``/``weight`` (models tend to add a ``description``,
    which fails the strict variant check); non-canonical rules — typically
    rollout-less, variant-assignment rules — are dropped, since experiment
    targeting lives in the top-level ``targeting`` field, not the flag rules.
    The top-level ``variants`` are left intact (the config service accepts an
    optional per-variant description on deploy).
    """
    flag_config = experiment.get("flag_config")
    if not isinstance(flag_config, dict):
        return

    variants = flag_config.get("variants")
    if isinstance(variants, list):
        flag_config["variants"] = [
            {field: variant[field] for field in _CANONICAL_VARIANT_FIELDS if field in variant}
            for variant in variants
            if isinstance(variant, dict)
        ]

    rules = flag_config.get("rules")
    if isinstance(rules, list):
        flag_config["rules"] = [rule for rule in rules if _is_canonical_rule(rule)]


def _designed_traffic_percentage(flag_config: dict[str, Any]) -> float:
    """The traffic share the safety validator judged for this design.

    Uses the validator's own helper (max over fallthrough AND rule rollouts) so
    deploy and blast-radius check read the identical number — reading only the
    fallthrough here left a bypass where a rules-only rollout passed the check
    at 10% and deployed at 100%. A design with no rollout anywhere cannot have
    passed the blast-radius gate; 100% is only a fallback for ungated callers.
    """
    percentage = _max_rollout_percentage(flag_config)
    if percentage is None:
        return 100.0
    return float(min(max(percentage, 0.0), 100.0))


async def deploy_experiment(project_id: str, experiment: dict[str, Any]) -> bool:
    """Create a running experiment (and its canonical backing flag) from a design.

    Config owns experiment→flag initialization, so this single call also creates
    the backing flag keyed by ``flag_key``. Shared by the agent (autonomy-permitting
    deploy) and the approval endpoint (human-approved deploy).
    """
    try:
        flag_config = experiment.get("flag_config", {})
        if not isinstance(flag_config, dict):
            flag_config = {}
        experiment_id = experiment.get("experiment_id", "")
        flag_key = flag_config.get("key") or experiment_id
        variants = experiment.get("variants") or flag_config.get("variants", [])
        description = experiment.get("description") or experiment.get("hypothesis", "")

        await create_experiment_config(
            project_id=project_id,
            experiment_id=experiment_id or flag_key,
            hypothesis=description,
            variants=variants,
            primary_metric=experiment.get("primary_metric", {}),
            secondary_metrics=experiment.get("secondary_metrics"),
            guardrail_metrics=experiment.get("guardrail_metrics"),
            targeting=experiment.get("targeting"),
            estimated_duration_days=experiment.get("estimated_duration_days", 14),
            flag_key=flag_key,
            traffic_percentage=_designed_traffic_percentage(flag_config),
        )
        logger.info("Experiment %s deployed successfully", experiment_id)
        return True
    except Exception as exc:
        logger.error("Failed to deploy experiment: %s", exc)
        return False


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
    # A small verification budget: enough to measure real baselines for the
    # events the design hinges on (sample-size math was previously guesswork
    # over "(to be determined)"), not enough to re-run behavior analysis.
    agentic_tools = ("discover_events", "query_events", "query_timeseries", "query_breakdown")
    max_tool_steps = 4

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
            baseline_metrics=(
                "(unknown — use your analytics tools to measure the current "
                "rate/volume of the primary metric event before sizing the experiment)"
            ),
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

        # LLMs drift from the canonical flag schema (descriptive variant fields,
        # rollout-less rules), which fails the validator's variant_config check.
        # Normalize before validating, deploying, and persisting so a sound
        # design is not halted on shape alone.
        _canonicalize_flag_config(output)

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
            deployed = await self._deploy(ctx, output)
            meta["deployed"] = deployed
            if not deployed:
                # deploy_experiment swallows HTTP failures into False; without
                # this the run would end "completed" while the auto-approved
                # experiment silently doesn't exist.
                message = f"experiment deploy failed: {meta['experiment_id'] or '(no id)'}"
                state.setdefault("errors", []).append(message)
                await ctx.audit.log(
                    ctx.run_id,
                    "deploy_failed",
                    {"experiment_id": meta["experiment_id"], "agent": self.name},
                )
        return meta

    def finalize(self, output: Any, action: dict[str, Any]) -> Any:
        # Store as a list so the supervisor's experiment count is uniform.
        return [output] if output else []

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_insight(insights: list[dict]) -> dict | None:
        insights = [i for i in insights if isinstance(i, dict)]
        if not insights:
            return None
        experiment_insights = [
            i
            for i in insights
            if i.get("action_type") == "experiment"
            # str() guards a present-but-null recommended_action, which would
            # crash build_prompt and fail the whole agent.
            or str(i.get("recommended_action") or "").lower().startswith("experiment")
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
            if not isinstance(parsed, dict):
                # Fail-open by design (a flaky fast-tier provider must not
                # block every experiment), but observably so.
                logger.warning("LLM safety review output was unparseable — review skipped")
                parsed = None
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
        # Config owns experiment→flag initialization. Delegates to the shared
        # deploy_experiment so the agent and the approval endpoint use one path.
        return await deploy_experiment(ctx.project_id, experiment)
