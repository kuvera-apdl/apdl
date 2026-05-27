"""Feature proposal agent — graph-based workflow.

Analyses experiment results and behavior patterns to propose concrete
new features with implementation specs. Always requires human approval,
even at the highest autonomy level.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, TypedDict

from app.graphs.runner import END, Graph
from app.llm.router import chat_completion
from app.llm.utils import parse_llm_json
from app.llm.prompts.feature import FEATURE_PROPOSAL_PROMPT, FEATURE_PROPOSAL_SYSTEM
from app.memory.pgvector_store import PgVectorStore
from app.tools.experiments import get_active_experiments, get_experiment_results

logger = logging.getLogger(__name__)


class FeatureProposalState(TypedDict, total=False):
    """State passed between nodes in the feature proposal graph."""
    project_id: str
    autonomy_level: int
    insights: list[dict]
    context: str
    experiment_results: list[dict]
    active_experiments: list[dict]
    proposals: list[dict]
    approved: bool | None
    error: str | None


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------

async def gather_experiment_data(state: FeatureProposalState) -> FeatureProposalState:
    """Fetch active experiments and their results."""
    project_id = state["project_id"]

    try:
        active = await get_active_experiments(project_id=project_id)
        state["active_experiments"] = active if isinstance(active, list) else []
    except Exception as exc:
        logger.warning("Could not fetch active experiments: %s", exc)
        state["active_experiments"] = []

    async def _fetch_result(exp: dict) -> dict[str, Any] | None:
        exp_id = exp.get("experiment_id", "")
        metric = exp.get("primary_metric", {}).get("event", "")
        if not exp_id or not metric:
            return None
        try:
            return await get_experiment_results(
                experiment_id=exp_id, metric=metric, project_id=project_id
            )
        except Exception as exc:
            logger.debug("Could not fetch results for %s: %s", exp_id, exc)
            return None

    fetched = await asyncio.gather(*[_fetch_result(e) for e in state.get("active_experiments", [])])
    state["experiment_results"] = [r for r in fetched if r is not None]
    return state


async def retrieve_context(state: FeatureProposalState) -> FeatureProposalState:
    """Retrieve historical context from vector memory."""
    vector_store: PgVectorStore | None = state.get("_vector_store")  # type: ignore[assignment]
    context_parts: list[str] = []

    if vector_store:
        try:
            memories = await vector_store.search(
                project_id=state["project_id"],
                query="feature proposals product capabilities experiment results",
                top_k=5,
            )
            context_parts = [m["content"] for m in memories]
        except Exception:
            pass

    state["context"] = "\n---\n".join(context_parts) if context_parts else ""
    return state


async def generate_proposals(state: FeatureProposalState) -> FeatureProposalState:
    """Use reasoning LLM to generate feature proposals."""
    prompt = FEATURE_PROPOSAL_PROMPT.format(
        experiment_results=json.dumps(state.get("experiment_results", []), default=str),
        insights=json.dumps(state.get("insights", []), default=str),
        context=state.get("context", ""),
        capabilities="(determined from project configuration)",
    )

    response = await chat_completion(
        model_tier="reasoning",
        messages=[
            {"role": "system", "content": FEATURE_PROPOSAL_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    proposals = parse_llm_json(response, [])
    if not isinstance(proposals, list):
        proposals = [proposals] if proposals else []

    state["proposals"] = proposals
    return state


async def approval_gate(state: FeatureProposalState) -> FeatureProposalState:
    """Human approval gate — feature proposals ALWAYS require human approval.

    This is a hard requirement regardless of autonomy level because feature
    proposals can have significant product and engineering impact.
    """
    if state.get("approved") is None:
        # Not yet approved — the supervisor should interrupt here
        state["approved"] = False
    return state


async def store_proposals(state: FeatureProposalState) -> FeatureProposalState:
    """Store approved proposals in vector memory for tracking."""
    vector_store: PgVectorStore | None = state.get("_vector_store")  # type: ignore[assignment]
    proposals = state.get("proposals", [])

    if vector_store and proposals and state.get("approved", False):
        for proposal in proposals:
            try:
                await vector_store.store(
                    project_id=state["project_id"],
                    content=json.dumps(proposal, default=str),
                    metadata={
                        "type": "feature_proposal",
                        "proposal_id": proposal.get("proposal_id", ""),
                        "priority": proposal.get("priority", "P2"),
                        "status": "approved",
                    },
                )
            except Exception as exc:
                logger.error("Failed to store proposal: %s", exc)

    return state


# --------------------------------------------------------------------------
# Conditional routing
# --------------------------------------------------------------------------

def route_after_approval(state: FeatureProposalState) -> str:
    """Route based on approval decision."""
    if state.get("approved", False):
        return "store_proposals"
    return "end"


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------

def build_feature_proposal_graph() -> Graph:
    """Build the feature proposal graph."""
    graph = Graph()

    graph.add_node("gather_experiment_data", gather_experiment_data)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("generate_proposals", generate_proposals)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("store_proposals", store_proposals)

    graph.set_entry_point("gather_experiment_data")
    graph.add_edge("gather_experiment_data", "retrieve_context")
    graph.add_edge("retrieve_context", "generate_proposals")
    graph.add_edge("generate_proposals", "approval_gate")

    graph.add_conditional_edges(
        "approval_gate",
        route_after_approval,
        {"store_proposals": "store_proposals", "end": END},
    )

    graph.add_edge("store_proposals", END)

    return graph


feature_proposal_graph = build_feature_proposal_graph()
