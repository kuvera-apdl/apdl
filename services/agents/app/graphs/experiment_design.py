"""Experiment design agent — graph-based workflow.

Takes insights from behavior analysis, designs experiments, validates
them through safety checks, and optionally deploys via feature flags
with human-in-the-loop approval.
"""

from __future__ import annotations

import json
import logging
from typing import TypedDict

from app.graphs.runner import END, Graph
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json
from app.llm.prompts.experiment import (
    EXPERIMENT_DESIGN_PROMPT,
    EXPERIMENT_DESIGN_SYSTEM,
    SAFETY_REVIEW_PROMPT,
)
from app.memory.pgvector_store import PgVectorStore
from app.safety.validator import AgentAction, ActionType, SafetyValidator
from app.tools.experiments import (
    create_experiment_config,
    get_active_experiments,
)
from app.tools.flags import create_flag

logger = logging.getLogger(__name__)
safety_validator = SafetyValidator()


class ExperimentDesignState(TypedDict, total=False):
    """State passed between nodes in the experiment design graph."""
    project_id: str
    autonomy_level: int
    insights: list[dict]       # input insights from behavior analysis
    context: str               # historical context
    active_experiments: list[dict]
    experiment_design: dict    # LLM-generated experiment design
    safety_result: dict        # safety validation result
    approved: bool | None      # human approval decision
    deployed: bool
    error: str | None


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------

async def get_context(state: ExperimentDesignState) -> ExperimentDesignState:
    """Retrieve context: active experiments and historical memory."""
    project_id = state["project_id"]

    # Fetch active experiments
    try:
        active = await get_active_experiments(project_id=project_id)
    except Exception as exc:
        logger.warning("Could not fetch active experiments: %s", exc)
        active = []
    state["active_experiments"] = active if isinstance(active, list) else []

    # Retrieve memory context
    vector_store: PgVectorStore | None = state.get("_vector_store")  # type: ignore[assignment]
    context_parts: list[str] = []
    if vector_store:
        try:
            memories = await vector_store.search(
                project_id=project_id,
                query="experiment results A/B test outcomes",
                top_k=5,
            )
            context_parts = [m["content"] for m in memories]
        except Exception:
            pass

    state["context"] = "\n---\n".join(context_parts) if context_parts else ""
    return state


async def design(state: ExperimentDesignState) -> ExperimentDesignState:
    """Use reasoning LLM to design an experiment based on insights."""
    insights = state.get("insights", [])
    if not insights:
        state["error"] = "No insights provided for experiment design."
        return state

    # Pick the highest-impact insight that recommends experimentation
    experiment_insights = [
        i for i in insights
        if i.get("action_type") == "experiment" or i.get("recommended_action", "").lower().startswith("experiment")
    ]
    if not experiment_insights:
        experiment_insights = insights[:1]  # fallback to first insight

    insight = experiment_insights[0]

    prompt = EXPERIMENT_DESIGN_PROMPT.format(
        insight=json.dumps(insight, default=str),
        context=state.get("context", ""),
        active_experiments=json.dumps(state.get("active_experiments", []), default=str),
        baseline_metrics="(to be determined from query data)",
    )

    response = await chat_completion(
        model_tier="reasoning",
        messages=[
            {"role": "system", "content": EXPERIMENT_DESIGN_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    design_obj = parse_llm_json(response)
    if design_obj is None:
        state["error"] = f"Failed to parse experiment design: {response[:200]}"
        return state

    state["experiment_design"] = design_obj
    return state


async def execute_safety(state: ExperimentDesignState) -> ExperimentDesignState:
    """Run safety validation on the proposed experiment."""
    experiment = state.get("experiment_design", {})
    if not experiment:
        state["safety_result"] = {"passed": False, "checks": [], "risk_level": "high"}
        return state

    action = AgentAction(
        type=ActionType.create_experiment,
        config=experiment,
        project_id=state["project_id"],
    )
    result = safety_validator.validate(action)
    state["safety_result"] = result.model_dump()

    # Also get LLM safety review for nuanced analysis
    try:
        llm_review_prompt = SAFETY_REVIEW_PROMPT.format(
            experiment=json.dumps(experiment, default=str),
            active_experiments=json.dumps(state.get("active_experiments", []), default=str),
        )
        llm_review = await chat_completion(
            model_tier="fast",
            messages=[
                {"role": "system", "content": "You are a safety reviewer for A/B experiments."},
                {"role": "user", "content": llm_review_prompt},
            ],
        )
        llm_result = parse_llm_json(llm_review)
        if llm_result and not llm_result.get("approved", True):
            state["safety_result"]["checks"].append({
                "name": "llm_safety_review",
                "passed": False,
                "message": "; ".join(llm_result.get("concerns", [])),
            })
            state["safety_result"]["passed"] = False
    except Exception as exc:
        logger.warning("LLM safety review failed: %s", exc)

    return state


async def approve(state: ExperimentDesignState) -> ExperimentDesignState:
    """Human-in-the-loop approval gate.

    The graph is interrupted here when autonomy_level requires approval.
    The supervisor resumes with the approval decision set in state.
    """
    # This node is an interrupt point — the supervisor will set 'approved'
    # when the human responds via the /approve endpoint.
    # If we reach here without an approval decision, default based on autonomy level.
    if state.get("approved") is None:
        autonomy = state.get("autonomy_level", 2)
        safety = state.get("safety_result", {})
        risk = safety.get("risk_level", "high")

        if autonomy >= 3 and safety.get("passed", False) and risk == "low":
            state["approved"] = True
        else:
            # Requires human approval — the graph should be interrupted before this
            state["approved"] = False

    return state


async def deploy(state: ExperimentDesignState) -> ExperimentDesignState:
    """Deploy the experiment by creating the flag and experiment config."""
    if not state.get("approved", False):
        state["deployed"] = False
        return state

    experiment = state.get("experiment_design", {})
    project_id = state["project_id"]

    try:
        # Create the feature flag
        flag_config = experiment.get("flag_config", {})
        flag_key = flag_config.get("key", experiment.get("experiment_id", "unknown"))

        await create_flag(
            project_id=project_id,
            key=flag_key,
            name=flag_config.get("name", flag_key),
            description=experiment.get("hypothesis", ""),
            enabled=True,
            default_value=False,
            rules=flag_config.get("rules", []),
            fallthrough=flag_config.get(
                "fallthrough",
                {
                    "value": True,
                    "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
                },
            ),
        )

        # Create the experiment configuration
        variants = experiment.get("variants", [])
        await create_experiment_config(
            project_id=project_id,
            experiment_id=experiment.get("experiment_id", flag_key),
            hypothesis=experiment.get("hypothesis", ""),
            variants=variants,
            primary_metric=experiment.get("primary_metric", {}),
            secondary_metrics=experiment.get("secondary_metrics"),
            guardrail_metrics=experiment.get("guardrail_metrics"),
            targeting=experiment.get("targeting"),
            estimated_duration_days=experiment.get("estimated_duration_days", 14),
            flag_key=flag_key,
        )

        state["deployed"] = True
        logger.info("Experiment %s deployed successfully", experiment.get("experiment_id"))
    except Exception as exc:
        logger.error("Failed to deploy experiment: %s", exc)
        state["deployed"] = False
        state["error"] = str(exc)

    return state


# --------------------------------------------------------------------------
# Conditional routing
# --------------------------------------------------------------------------

def route_after_safety(state: ExperimentDesignState) -> str:
    """Route based on safety result and autonomy level."""
    safety = state.get("safety_result", {})
    autonomy = state.get("autonomy_level", 2)

    if not safety.get("passed", False):
        return "end"  # failed safety — do not proceed

    if autonomy <= 1:
        return "end"  # L1: suggest only, never deploy

    risk = safety.get("risk_level", "high")

    if autonomy >= 3 and risk == "low":
        return "deploy"  # L3+: auto-deploy low-risk
    elif autonomy >= 2:
        return "approve"  # L2+: go to approval gate
    else:
        return "end"


def route_after_approval(state: ExperimentDesignState) -> str:
    """Route based on approval decision."""
    if state.get("approved", False):
        return "deploy"
    return "end"


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------

def build_experiment_design_graph() -> Graph:
    """Build the experiment design graph."""
    graph = Graph()

    graph.add_node("get_context", get_context)
    graph.add_node("design", design)
    graph.add_node("safety", execute_safety)
    graph.add_node("approve", approve)
    graph.add_node("deploy", deploy)

    graph.set_entry_point("get_context")
    graph.add_edge("get_context", "design")
    graph.add_edge("design", "safety")

    graph.add_conditional_edges(
        "safety",
        route_after_safety,
        {"deploy": "deploy", "approve": "approve", "end": END},
    )

    graph.add_conditional_edges(
        "approve",
        route_after_approval,
        {"deploy": "deploy", "end": END},
    )

    graph.add_edge("deploy", END)

    return graph


experiment_design_graph = build_experiment_design_graph()
