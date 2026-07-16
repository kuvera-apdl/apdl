"""Experiment design agent.

Consumes behaviour-analysis insights, designs an A/B experiment, runs it
through the safety validator (plus a nuanced LLM safety review), and routes
every valid design to human approval. Approval creates an inert Config draft
before opening the treatment changeset; activation is a separate lifecycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

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
from app.models.experiment_design import ExperimentDesign, ExperimentSafetyReview
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
from app.tools.experiments import (
    create_experiment_draft as create_config_experiment_draft,
    get_active_experiments,
)

logger = logging.getLogger(__name__)
_safety = SafetyValidator()


def _designed_traffic_percentage(flag_config: dict[str, Any]) -> float:
    """The traffic share the safety validator judged for this design.

    Uses the validator's own helper so the Config draft records the exact
    traffic proposal that the blast-radius check judged. A design with no
    rollout anywhere cannot have passed the blast-radius gate; 100% is only a
    fallback for direct callers.
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

    The treatment is built behind the disabled flag created with the Config
    draft, while the control path stays untouched. ``treatment_spec`` is the
    design's canonical work order. An empty string explicitly declares a
    config-only experiment and therefore produces no changeset.
    """
    experiment_id = str(design.get("experiment_id") or "").strip()
    flag_key = _flag_key_of(design)
    if not flag_key:
        return None

    what_to_build = str(design.get("treatment_spec") or "").strip()
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
        f"Backing feature flag: `{flag_key}` (disabled Config draft; variants: {variants or 'control, treatment'})",
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
    """Open the codegen changeset that implements a drafted design's treatment.

    Returns the changeset id ("" when the design needs no code). This is called
    only after the human-approved Config draft has been created.
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
        context={
            "experiment_id": str(design.get("experiment_id") or ""),
            "flag_key": _flag_key_of(design),
        },
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


async def stage_experiment_draft(project_id: str, experiment: dict[str, Any]) -> bool:
    """Create an inert experiment draft and its disabled backing flag.

    Config owns experiment→flag initialization, so this single call also creates
    the backing flag keyed by ``flag_key``. Only the approval endpoint calls
    this function; experiment-design agents never apply their own proposals.
    """
    try:
        flag_config = experiment.get("flag_config", {})
        if not isinstance(flag_config, dict):
            flag_config = {}
        experiment_id = experiment.get("experiment_id", "")
        flag_key = flag_config.get("key") or experiment_id
        variants = experiment.get("variants") or flag_config.get("variants", [])
        description = experiment.get("description") or experiment.get("hypothesis", "")

        await create_config_experiment_draft(
            project_id=project_id,
            experiment_id=experiment_id or flag_key,
            hypothesis=description,
            variants=variants,
            default_variant=flag_config.get("default_variant"),
            primary_metric=experiment.get("primary_metric", {}),
            statistical_plan=experiment.get("statistical_plan"),
            targeting=experiment.get("targeting"),
            flag_key=flag_key,
            traffic_percentage=_designed_traffic_percentage(flag_config),
        )
        logger.info("Experiment %s drafted successfully", experiment_id)
        return True
    except Exception as exc:
        logger.error("Failed to create experiment draft: %s", exc)
        return False


_LIVE_EXPERIMENT_STATUSES = {"scheduled", "running"}
_EXPERIMENT_STATUSES = _LIVE_EXPERIMENT_STATUSES | {"draft", "completed", "stopped"}
_TARGETING_OPERATORS = {
    "equals", "not_equals", "gt", "gte", "lt", "lte", "contains",
    "not_contains", "starts_with", "ends_with", "in", "not_in", "exists",
    "not_exists",
}


def _validated_conditions(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    for condition in value:
        if not isinstance(condition, dict):
            return None
        attribute = condition.get("attribute")
        operator = condition.get("operator")
        if not isinstance(attribute, str) or not attribute or operator not in _TARGETING_OPERATORS:
            return None
        if operator in {"exists", "not_exists"}:
            if set(condition) != {"attribute", "operator"}:
                return None
        elif set(condition) != {"attribute", "operator", "value"} or condition.get("value") is None:
            return None
    return value


def _conjunctions_are_disjoint(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> bool:
    """Prove disjointness for contradictory equality constraints.

    Returning false means the populations may overlap, not that they are
    identical. This conservative relation is sufficient for fail-closed
    primary-metric conflict detection.
    """
    left_equals = {
        condition["attribute"]: condition["value"]
        for condition in left
        if condition["operator"] == "equals"
    }
    right_equals = {
        condition["attribute"]: condition["value"]
        for condition in right
        if condition["operator"] == "equals"
    }
    return any(
        left_equals[attribute] != right_equals[attribute]
        for attribute in left_equals.keys() & right_equals.keys()
    )


def _populations_may_overlap(
    design_conditions: Any, active_rules: Any
) -> bool | None:
    design = _validated_conditions(design_conditions)
    if design is None or not isinstance(active_rules, list):
        return None
    if not design or not active_rules:
        return True

    # Config rules are OR branches. The two populations may overlap when any
    # active branch is not provably disjoint from the design conjunction.
    for rule in active_rules:
        if not isinstance(rule, dict):
            return None
        conditions = _validated_conditions(rule.get("conditions"))
        if conditions is None:
            return None
        if not _conjunctions_are_disjoint(design, conditions):
            return True
    return False


def _config_conflict_check(
    design: dict[str, Any],
    config_rows: list[dict[str, Any]],
    evidence: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Compare a design with authoritative Config experiment records."""
    if not evidence or evidence.get("status") != "available":
        return ({
            "name": "config_conflicts",
            "passed": None,
            "status": "unavailable",
            "message": "Config conflicts could not be evaluated; human review is required.",
        }, False)

    experiment_id = str(design.get("experiment_id") or "")
    flag_key = _flag_key_of(design)
    metric = design.get("primary_metric")
    metric_event = metric.get("event") if isinstance(metric, dict) else None
    targeting = design.get("targeting")
    design_conditions = targeting.get("conditions") if isinstance(targeting, dict) else None

    conflicts: list[str] = []
    incomplete: list[str] = []
    for index, row in enumerate(config_rows):
        if not isinstance(row, dict):
            incomplete.append(f"row {index} is not an object")
            continue
        key = row.get("key")
        existing_flag_key = row.get("flag_key")
        status = row.get("status")
        if not isinstance(key, str) or not key:
            incomplete.append(f"row {index} has no experiment key")
            continue
        if not isinstance(existing_flag_key, str) or not existing_flag_key:
            incomplete.append(f"experiment {key} has no flag key")
        if status not in _EXPERIMENT_STATUSES:
            incomplete.append(f"experiment {key} has an unknown status")

        if experiment_id and key == experiment_id:
            conflicts.append(f"experiment id {experiment_id!r} already exists")
        if flag_key and existing_flag_key == flag_key:
            conflicts.append(f"flag key {flag_key!r} already exists")

        if status not in _LIVE_EXPERIMENT_STATUSES:
            continue
        active_metric = row.get("primary_metric")
        active_event = active_metric.get("event") if isinstance(active_metric, dict) else None
        if not isinstance(active_event, str) or not active_event:
            incomplete.append(f"active experiment {key} has no primary metric event")
            continue
        if not isinstance(metric_event, str) or not metric_event:
            incomplete.append("design has no primary metric event")
            continue
        if active_event != metric_event:
            continue
        if "targeting_rules" not in row:
            incomplete.append(f"active experiment {key} has no targeting rules evidence")
            continue
        overlap = _populations_may_overlap(design_conditions, row.get("targeting_rules"))
        if overlap is None:
            incomplete.append(f"active experiment {key} has unevaluable targeting")
        elif overlap:
            conflicts.append(
                f"primary metric {metric_event!r} may overlap population with {key!r}"
            )

    if conflicts:
        message = "; ".join(dict.fromkeys(conflicts))
        if incomplete:
            message += "; additional Config rows were unevaluable"
        return ({
            "name": "config_conflicts",
            "passed": False,
            "status": "available" if not incomplete else "partial",
            "message": message,
        }, not incomplete)
    if incomplete:
        return ({
            "name": "config_conflicts",
            "passed": None,
            "status": "partial",
            "message": (
                "Config conflict evidence is incomplete: "
                + "; ".join(dict.fromkeys(incomplete))
                + "; human review is required."
            ),
        }, False)
    return ({
        "name": "config_conflicts",
        "passed": True,
        "status": "available",
        "message": (
            "No duplicate identity or overlapping primary-metric population "
            "conflict was found in Config records."
        ),
    }, True)


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
    """Designs and validates experiments for an explicit human gate."""

    name = "experiment_design"
    description = "Design A/B experiments from insights for human approval."
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
    agentic_tools = (
        "discover_events",
        "query_events",
        "query_timeseries",
        "query_breakdown",
        "calculate_statistical_plan",
    )
    max_tool_steps = 6
    #: Upper bound on designs per run — at most one per qualifying insight;
    #: insights that don't warrant an experiment get none.
    max_designs = 3

    def parse(self, response: str) -> list[dict[str, Any]]:
        """Reject malformed model output; never coerce or silently repair it."""
        raw = parse_llm_json(response, fallback=None)
        if not isinstance(raw, list):
            raise ValueError("experiment design output must be a JSON array")

        designs: list[ExperimentDesign] = []
        for index, item in enumerate(raw):
            try:
                designs.append(ExperimentDesign.model_validate(item))
            except ValidationError as exc:
                details = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors(include_url=False)[:5]
                )
                raise ValueError(
                    f"invalid experiment design at index {index}: {details}"
                ) from exc

        experiment_ids = [design.experiment_id for design in designs]
        if len(set(experiment_ids)) != len(experiment_ids):
            raise ValueError("experiment designs must contain unique experiment_id values")
        flag_keys = [design.flag_config.key for design in designs]
        if len(set(flag_keys)) != len(flag_keys):
            raise ValueError("experiment designs must contain unique flag_config.key values")
        return [
            design.model_dump(mode="json", exclude_none=True)
            for design in designs
        ]

    async def gather(
        self, ctx: AgentContext, state: dict[str, Any], working: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            active = await get_active_experiments(project_id=ctx.project_id)
            if not isinstance(active, list):
                raise TypeError("Config experiment response was not a list")
            active_evidence = {
                "status": "available",
                "source": "config",
                "message": "Active experiment evidence loaded from Config.",
            }
        except Exception as exc:
            logger.warning("Could not fetch active experiments: %s", exc)
            active = []
            active_evidence = {
                "status": "unavailable",
                "source": "config",
                "message": "Active experiment evidence is unavailable.",
            }
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
            "active_experiments": active,
            "active_experiments_evidence": active_evidence,
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
        for design in output:
            safety = await self._safety_check(
                ctx,
                design,
                active,
                working.get("active_experiments_evidence"),
            )
            decision = gate_action(
                ctx.autonomy_level,
                safety,
                always_require_approval=True,
            )
            # Stamped onto the persisted item so the approval gate can tell an
            # approvable design from a halted sibling, and
            # the console can render per-design outcomes.
            design["decision"] = decision.value
            design["safety_result"] = safety
            design["deployed"] = False
            await self._record(ctx, design, decision)

        ids = [d.get("experiment_id", "") for d in output]
        return {
            # Singular kept for audit/console back-compat with single-design runs.
            "experiment_id": ids[0] if ids else "",
            "experiment_ids": ids,
            "deployed_count": 0,
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

    async def _record(
        self, ctx: AgentContext, design: dict[str, Any], decision: GateDecision
    ) -> None:
        """Best-effort ledger write; a failure must not fail the run."""
        if ctx.pool is None:
            return
        if decision is GateDecision.approve:
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
        self,
        ctx: AgentContext,
        experiment: dict,
        active_experiments: list[dict],
        active_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = AgentAction(
            type=ActionType.create_experiment,
            config=experiment,
            project_id=ctx.project_id,
        )
        result = _safety.validate(action).model_dump()
        result["evidence_complete"] = True
        # Experiment creation is always a preview requiring human approval,
        # even when all deterministic and external evidence is available.
        result["requires_approval"] = True

        if active_evidence and active_evidence.get("status") == "available":
            result["checks"].append({
                "name": "active_experiments_evidence",
                "passed": True,
                "status": "available",
                "message": active_evidence.get("message", "Config evidence is available."),
            })
        else:
            result["checks"].append({
                "name": "active_experiments_evidence",
                "passed": None,
                "status": "unavailable",
                "message": "Active experiment evidence is unavailable; human review is required.",
            })
            result["evidence_complete"] = False

        conflict_check, conflict_evidence_complete = _config_conflict_check(
            experiment, active_experiments, active_evidence
        )
        result["checks"].append(conflict_check)
        if conflict_check["passed"] is False:
            result["passed"] = False
        if not conflict_evidence_complete:
            result["evidence_complete"] = False

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
            parsed = ExperimentSafetyReview.model_validate(parse_llm_json(review))
            if not parsed.approved:
                result["checks"].append({
                    "name": "llm_safety_review",
                    "passed": False,
                    "status": "available",
                    "message": "; ".join(parsed.concerns) or "LLM safety review rejected the design.",
                })
                result["passed"] = False
            else:
                result["checks"].append({
                    "name": "llm_safety_review",
                    "passed": True,
                    "status": "available",
                    "message": "LLM safety review approved the design.",
                })
            risk_order = {"low": 0, "medium": 1, "high": 2}
            if risk_order[parsed.risk_level] > risk_order.get(result["risk_level"], 2):
                result["risk_level"] = parsed.risk_level
        except Exception:
            logger.warning("LLM safety review was unavailable or invalid")
            result["checks"].append({
                "name": "llm_safety_review",
                "passed": None,
                "status": "unavailable",
                "message": "LLM safety review is unavailable; human review is required.",
            })
            result["evidence_complete"] = False

        return result
