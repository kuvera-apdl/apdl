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
from app.store.experiments import (
    insight_key,
    link_changeset,
    list_designed_experiments,
    record_designed_experiment,
)
from app.tools.code import open_changeset
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


def _flag_key_of(design: dict[str, Any]) -> str:
    flag = design.get("flag_config")
    key = flag.get("key") if isinstance(flag, dict) else ""
    return str(key or design.get("experiment_id") or "").strip()


def treatment_changeset_task(design: dict[str, Any]) -> tuple[str, str] | None:
    """Build the (title, spec) for the codegen changeset implementing a design's
    treatment variant, or None when the design declares no code is needed.

    A deployed experiment without treatment code measures noise — both variants
    render the same product. This spec is what makes the experiment real: the
    treatment is built behind the already-deployed flag, the control path stays
    untouched. ``treatment_spec`` is the design's own work order; a missing
    field (older designs, model omission) degrades to the hypothesis + variant
    descriptions rather than silently skipping the build. An explicitly empty
    string is the design saying "config-only experiment" — no changeset.
    """
    experiment_id = str(design.get("experiment_id") or "").strip()
    flag_key = _flag_key_of(design)
    if not flag_key:
        return None

    if "treatment_spec" in design:
        what_to_build = str(design.get("treatment_spec") or "").strip()
        if not what_to_build:
            return None
    else:
        hypothesis = str(design.get("hypothesis") or "").strip()
        treatments = [
            f"- {v.get('key')}: {v.get('description')}"
            for v in design.get("variants") or []
            if isinstance(v, dict) and v.get("key") != "control" and v.get("description")
        ]
        what_to_build = "\n".join(
            part for part in (hypothesis, *treatments) if part
        ).strip()
        if not what_to_build:
            return None

    metric_event = str((design.get("primary_metric") or {}).get("event") or "").strip()
    variants = ", ".join(
        f"{v.get('key')} (weight {v.get('weight')})"
        for v in design.get("variants") or []
        if isinstance(v, dict) and v.get("key")
    )

    title = f"Implement treatment for experiment {experiment_id or flag_key}"
    sections = [
        "## Experiment",
        f"Hypothesis: {design.get('hypothesis', '')}",
        f"Backing feature flag: `{flag_key}` (already deployed; variants: {variants or 'control, treatment'})",
    ]
    if metric_event:
        sections.append(f"Primary metric event: `{metric_event}`")
    sections += [
        "",
        "## What to build",
        what_to_build,
        "",
        "## Flag integration requirements",
        f"- Gate ALL treatment behavior behind the APDL feature flag `{flag_key}` using the "
        "APDL SDK already integrated in this repository: users assigned \"control\" see the "
        "current behavior unchanged; users assigned \"treatment\" see the new behavior.",
        "- Do not remove or modify the control code path.",
    ]
    if metric_event:
        sections.append(
            f"- Ensure the primary metric event `{metric_event}` fires on the relevant "
            "action for both variants."
        )
    sections += [
        "",
        "## Acceptance criteria",
        "- With flag variant \"control\": behavior identical to today.",
        "- With flag variant \"treatment\": the change described above is visible and reachable.",
        "- All existing tests pass.",
    ]
    return title, "\n".join(sections)


async def open_treatment_changeset(
    pool: Any, project_id: str, run_id: str, design: dict[str, Any]
) -> str:
    """Open the codegen changeset that implements a deployed design's treatment.

    Returns the changeset id ("" when the design needs no code or the open
    failed — callers surface that, they don't crash the deploy that already
    succeeded). Shared by the agent (autonomy-permitting deploy) and the
    approval endpoint (human-approved deploy), like deploy_experiment.
    """
    task = treatment_changeset_task(design)
    if task is None:
        return ""
    title, spec = task
    changeset = await open_changeset(
        project_id=project_id,
        title=title,
        spec=spec,
        run_id=run_id,
        constraints=[
            "All existing tests must pass.",
            "Do not modify or remove the control code path.",
        ],
    )
    changeset_id = str(changeset.get("changeset_id") or "").strip()
    if changeset_id and pool is not None:
        try:
            await link_changeset(
                pool, project_id, str(design.get("experiment_id") or _flag_key_of(design)),
                changeset_id,
            )
        except Exception as exc:
            logger.warning(
                "Could not link changeset %s to experiment %s: %s",
                changeset_id, design.get("experiment_id"), exc,
            )
    return changeset_id


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


def _render_designed(designed: list[dict[str, Any]]) -> str:
    """One line per prior design for the prompt's do-not-redesign section."""
    lines = [
        f"- {d.get('experiment_id', '?')}: {d.get('hypothesis') or d.get('title') or '?'}"
        f" ({d.get('status', '?')})"
        for d in designed
    ]
    return "\n".join(lines) if lines else "(none)"


@register_agent
class ExperimentDesignAgent(BaseAgent):
    """Designs, validates, and (autonomy permitting) deploys experiments."""

    name = "experiment_design"
    description = "Design A/B experiments from insights and deploy them safely."
    order = 20
    system_prompt = EXPERIMENT_DESIGN_SYSTEM
    model_tier = "reasoning"
    memory_query = "experiment results A/B test outcomes"
    memory_top_k = 5
    requires = ("insights",)
    produces = "experiment_designs"
    parse_as = "list"
    # A verification budget: enough to measure real baselines for the events
    # each design hinges on (sample-size math was previously guesswork over
    # "(to be determined)"), not enough to re-run behavior analysis.
    agentic_tools = ("discover_events", "query_events", "query_timeseries", "query_breakdown")
    max_tool_steps = 6
    #: Upper bound on designs per run — at most one per qualifying insight;
    #: insights that don't warrant an experiment get none.
    max_designs = 3

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            active = await get_active_experiments(project_id=ctx.project_id)
        except Exception as exc:
            logger.warning("Could not fetch active experiments: %s", exc)
            active = []
        # The durable design ledger is the hard dedup layer: insights barely
        # change between runs, so without it every run redesigns the same
        # experiments (only softly guarded by active_experiments in the prompt).
        designed: list[dict[str, Any]] = []
        if ctx.pool is not None:
            try:
                designed = await list_designed_experiments(ctx.pool, ctx.project_id)
            except Exception as exc:
                logger.warning("Could not list designed experiments: %s", exc)
        return {
            "active_experiments": active if isinstance(active, list) else [],
            "designed_experiments": designed,
        }

    def build_prompt(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> str | None:
        insights = self._select_insights(
            state.get("insights", []), working.get("designed_experiments", [])
        )
        if not insights:
            return None
        return EXPERIMENT_DESIGN_PROMPT.format(
            insights=json.dumps(insights, default=str),
            context=working.get("context", ""),
            active_experiments=json.dumps(working.get("active_experiments", []), default=str),
            designed_experiments=_render_designed(working.get("designed_experiments", [])),
            baseline_metrics=(
                "(unknown — use your analytics tools to measure the current "
                "rate/volume of each primary metric event before sizing the experiments)"
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
            return {"experiment_id": "", "experiment_ids": [], "deployed_count": 0,
                    "needs_approval": False}

        active = working.get("active_experiments", [])
        deployed_count = 0
        for design in output:
            # LLMs drift from the canonical flag schema (descriptive variant
            # fields, rollout-less rules), which fails the validator's
            # variant_config check. Normalize before validating, deploying,
            # and persisting so a sound design is not halted on shape alone.
            _canonicalize_flag_config(design)

            safety = await self._safety_check(ctx, design, active)
            decision = gate_action(ctx.autonomy_level, safety)
            # Stamped onto the persisted item so the approval gate can tell an
            # approvable design from a halted or already-deployed sibling, and
            # the console can render per-design outcomes.
            design["decision"] = decision.value
            design["safety_result"] = safety
            design["deployed"] = False

            if decision is GateDecision.deploy:
                deployed = await self._deploy(ctx, design)
                design["deployed"] = deployed
                if deployed:
                    deployed_count += 1
                    await self._open_treatment(ctx, state, design)
                else:
                    # deploy_experiment swallows HTTP failures into False;
                    # without this the run would end "completed" while the
                    # auto-approved experiment silently doesn't exist.
                    experiment_id = design.get("experiment_id", "")
                    message = f"experiment deploy failed: {experiment_id or '(no id)'}"
                    state.setdefault("errors", []).append(message)
                    await ctx.audit.log(
                        ctx.run_id,
                        "deploy_failed",
                        {"experiment_id": experiment_id, "agent": self.name},
                    )
            await self._record(ctx, design, decision)

        ids = [d.get("experiment_id", "") for d in output]
        return {
            # Singular kept for audit/console back-compat with single-design runs.
            "experiment_id": ids[0] if ids else "",
            "experiment_ids": ids,
            "deployed_count": deployed_count,
            "needs_approval": any(d.get("decision") == GateDecision.approve.value for d in output),
        }

    # ------------------------------------------------------------------
    # Internal steps
    # ------------------------------------------------------------------

    def _select_insights(
        self, insights: list[dict], designed: list[dict[str, Any]]
    ) -> list[dict]:
        """Experiment-worthy insights not already covered by the design ledger."""
        insights = [i for i in insights if isinstance(i, dict)]
        experiment_insights = [
            i
            for i in insights
            if i.get("action_type") == "experiment"
            # str() guards a present-but-null recommended_action, which would
            # crash build_prompt and fail the whole agent.
            or str(i.get("recommended_action") or "").lower().startswith("experiment")
        ]
        selected = experiment_insights or insights
        # 'iterate_requested' rows are released deliberately: the evaluation
        # agent concluded the experiment inconclusive-but-promising, so its
        # insight may be redesigned (with the learning in retrieved memory).
        covered = {
            d.get("insight_key", "")
            for d in designed
            if d.get("status") != "iterate_requested"
        } - {""}
        fresh = [i for i in selected if insight_key(i) not in covered]
        return fresh[: self.max_designs]

    async def _open_treatment(
        self, ctx: AgentContext, state: dict[str, Any], design: dict[str, Any]
    ) -> None:
        """Open the treatment changeset for a just-deployed design.

        A failure is surfaced (state error + audit) but never unwinds the
        deploy that already succeeded — the experiment exists either way; what
        failed is making its treatment real, and a human can retry from the
        console. Without the surfacing, the experiment silently measures noise.
        """
        experiment_id = str(design.get("experiment_id") or "")
        try:
            changeset_id = await open_treatment_changeset(
                ctx.pool, ctx.project_id, ctx.run_id, design
            )
        except Exception as exc:
            logger.error("Treatment changeset for %s failed: %s", experiment_id, exc)
            state.setdefault("errors", []).append(
                f"treatment changeset failed: {experiment_id or '(no id)'}"
            )
            await ctx.audit.log(
                ctx.run_id,
                "treatment_changeset_failed",
                {"experiment_id": experiment_id, "agent": self.name, "error": str(exc)},
            )
            return
        design["treatment_changeset_id"] = changeset_id
        if changeset_id:
            await ctx.audit.log(
                ctx.run_id,
                "treatment_changeset_opened",
                {"experiment_id": experiment_id, "changeset_id": changeset_id},
            )

    async def _record(
        self, ctx: AgentContext, design: dict[str, Any], decision: GateDecision
    ) -> None:
        """Best-effort ledger write; a failure must not fail the run."""
        if ctx.pool is None:
            return
        if design.get("deployed"):
            status = "deployed"
        elif decision is GateDecision.approve:
            status = "awaiting_approval"
        else:
            status = "halted"
        try:
            await record_designed_experiment(
                ctx.pool, ctx.project_id, ctx.run_id, design, status
            )
        except Exception as exc:
            logger.warning(
                "Could not record designed experiment %s: %s",
                design.get("experiment_id", "?"), exc,
            )

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
