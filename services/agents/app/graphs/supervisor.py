"""Supervisor orchestration graph — coordinates all agent sub-graphs.

The supervisor decides which agents to invoke based on the trigger
configuration, passes state between them, and manages the overall run
lifecycle with PostgreSQL-backed checkpointing.
"""

from __future__ import annotations

import logging
import traceback
from typing import TypedDict

import asyncpg

from app.graphs.behavior_analysis import behavior_analysis_graph
from app.graphs.experiment_design import experiment_design_graph
from app.graphs.feature_proposal import feature_proposal_graph
from app.graphs.personalization import personalization_graph
from app.memory.pgvector_store import PgVectorStore
from app.safety.audit import AuditLogger

logger = logging.getLogger(__name__)


class SupervisorState(TypedDict, total=False):
    """Top-level state for the supervisor orchestration."""
    run_id: str
    project_id: str
    autonomy_level: int
    analysis_types: list[str]
    time_range_days: int
    insights: list[dict]
    experiment_designs: list[dict]
    personalizations: list[dict]
    feature_proposals: list[dict]
    current_phase: str
    errors: list[str]


async def _update_run(
    pool: asyncpg.Pool,
    run_id: str,
    status: str,
    phase: str,
    insights_count: int = 0,
    experiments_count: int = 0,
) -> None:
    """Update the agent_runs table with current progress."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs
            SET status = $2, phase = $3, insights_count = $4,
                experiments_count = $5, updated_at = now()
            WHERE run_id = $1
            """,
            run_id,
            status,
            phase,
            insights_count,
            experiments_count,
        )


async def run_supervisor(
    pool: asyncpg.Pool,
    vector_store: PgVectorStore,
    run_id: str,
    project_id: str,
    analysis_types: list[str],
    time_range_days: int,
    autonomy_level: int,
) -> None:
    """Execute the supervisor orchestration.

    This function is invoked as a background task by the trigger endpoint.
    It runs each requested agent sub-graph in sequence, passing insights
    forward between stages.

    The flow is:
    1. behavior_analysis -> produces insights
    2. experiment_design -> consumes insights, produces experiment designs
    3. personalization -> consumes insights, produces UI configs
    4. feature_proposal -> consumes insights + experiment results, produces proposals
    """
    audit = AuditLogger(pool)
    state = SupervisorState(
        run_id=run_id,
        project_id=project_id,
        autonomy_level=autonomy_level,
        analysis_types=analysis_types,
        time_range_days=time_range_days,
        insights=[],
        experiment_designs=[],
        personalizations=[],
        feature_proposals=[],
        current_phase="starting",
        errors=[],
    )

    await audit.log(run_id, "supervisor_start", {
        "analysis_types": analysis_types,
        "autonomy_level": autonomy_level,
        "time_range_days": time_range_days,
    })

    try:
        # ---- Phase 1: Behavior Analysis ----
        if "behavior_analysis" in analysis_types:
            await _update_run(pool, run_id, "running", "behavior_analysis")
            logger.info("[%s] Starting behavior analysis", run_id)

            try:
                analysis_state = {
                    "project_id": project_id,
                    "time_range_days": time_range_days,
                    "_vector_store": vector_store,
                }
                result = await behavior_analysis_graph.ainvoke(analysis_state)
                insights = result.get("insights", [])
                state["insights"] = insights

                await audit.log(run_id, "behavior_analysis_complete", {
                    "insights_count": len(insights),
                })
                logger.info("[%s] Behavior analysis produced %d insights", run_id, len(insights))
            except Exception as exc:
                error_msg = f"Behavior analysis failed: {exc}"
                logger.error("[%s] %s", run_id, error_msg)
                state["errors"].append(error_msg)
                await audit.log(run_id, "behavior_analysis_error", {"error": str(exc)})

        # ---- Phase 2: Experiment Design ----
        if "experiment_design" in analysis_types and state["insights"]:
            await _update_run(
                pool, run_id, "running", "experiment_design",
                insights_count=len(state["insights"]),
            )
            logger.info("[%s] Starting experiment design", run_id)

            try:
                exp_state = {
                    "project_id": project_id,
                    "autonomy_level": autonomy_level,
                    "insights": state["insights"],
                    "_vector_store": vector_store,
                }
                result = await experiment_design_graph.ainvoke(exp_state)

                design = result.get("experiment_design", {})
                if design:
                    state["experiment_designs"].append(design)

                deployed = result.get("deployed", False)
                await audit.log(run_id, "experiment_design_complete", {
                    "experiment_id": design.get("experiment_id", ""),
                    "deployed": deployed,
                    "safety_result": result.get("safety_result", {}),
                })

                # If approval is needed, pause the run
                if not result.get("approved") and result.get("safety_result", {}).get("passed"):
                    if autonomy_level < 3 or result.get("safety_result", {}).get("risk_level") != "low":
                        await _update_run(
                            pool, run_id, "waiting_approval", "experiment_approval",
                            insights_count=len(state["insights"]),
                            experiments_count=len(state["experiment_designs"]),
                        )
                        await audit.log(run_id, "waiting_approval", {
                            "experiment_id": design.get("experiment_id", ""),
                        })
                        logger.info("[%s] Waiting for human approval", run_id)
                        # In a full implementation, we'd suspend here and resume
                        # when the approval endpoint is called. For now, we continue
                        # with the remaining non-blocking agents.

                logger.info("[%s] Experiment design complete", run_id)
            except Exception as exc:
                error_msg = f"Experiment design failed: {exc}"
                logger.error("[%s] %s", run_id, error_msg)
                state["errors"].append(error_msg)
                await audit.log(run_id, "experiment_design_error", {"error": str(exc)})

        # ---- Phase 3: Personalization ----
        if "personalization" in analysis_types and state["insights"]:
            await _update_run(
                pool, run_id, "running", "personalization",
                insights_count=len(state["insights"]),
                experiments_count=len(state["experiment_designs"]),
            )
            logger.info("[%s] Starting personalization", run_id)

            try:
                pers_state = {
                    "project_id": project_id,
                    "autonomy_level": autonomy_level,
                    "insights": state["insights"],
                    "_vector_store": vector_store,
                }
                result = await personalization_graph.ainvoke(pers_state)
                configs = result.get("ui_configs", [])
                state["personalizations"] = configs

                await audit.log(run_id, "personalization_complete", {
                    "configs_generated": len(configs),
                    "configs_deployed": result.get("deployed_count", 0),
                })
                logger.info(
                    "[%s] Personalization generated %d configs, deployed %d",
                    run_id, len(configs), result.get("deployed_count", 0),
                )
            except Exception as exc:
                error_msg = f"Personalization failed: {exc}"
                logger.error("[%s] %s", run_id, error_msg)
                state["errors"].append(error_msg)
                await audit.log(run_id, "personalization_error", {"error": str(exc)})

        # ---- Phase 4: Feature Proposal ----
        if "feature_proposal" in analysis_types and state["insights"]:
            await _update_run(
                pool, run_id, "running", "feature_proposal",
                insights_count=len(state["insights"]),
                experiments_count=len(state["experiment_designs"]),
            )
            logger.info("[%s] Starting feature proposal", run_id)

            try:
                fp_state = {
                    "project_id": project_id,
                    "autonomy_level": autonomy_level,
                    "insights": state["insights"],
                    "_vector_store": vector_store,
                }
                result = await feature_proposal_graph.ainvoke(fp_state)
                proposals = result.get("proposals", [])
                state["feature_proposals"] = proposals

                await audit.log(run_id, "feature_proposal_complete", {
                    "proposals_count": len(proposals),
                })
                logger.info("[%s] Feature proposal generated %d proposals", run_id, len(proposals))
            except Exception as exc:
                error_msg = f"Feature proposal failed: {exc}"
                logger.error("[%s] %s", run_id, error_msg)
                state["errors"].append(error_msg)
                await audit.log(run_id, "feature_proposal_error", {"error": str(exc)})

        # ---- Completion ----
        final_status = "completed" if not state["errors"] else "completed_with_errors"
        await _update_run(
            pool, run_id, final_status, "done",
            insights_count=len(state["insights"]),
            experiments_count=len(state["experiment_designs"]),
        )

        await audit.log(run_id, "supervisor_complete", {
            "status": final_status,
            "insights_count": len(state["insights"]),
            "experiments_count": len(state["experiment_designs"]),
            "personalizations_count": len(state["personalizations"]),
            "proposals_count": len(state["feature_proposals"]),
            "errors": state["errors"],
        })

        logger.info(
            "[%s] Supervisor complete: %d insights, %d experiments, %d personalizations, %d proposals",
            run_id,
            len(state["insights"]),
            len(state["experiment_designs"]),
            len(state["personalizations"]),
            len(state["feature_proposals"]),
        )

    except Exception as exc:
        logger.error("[%s] Supervisor failed: %s\n%s", run_id, exc, traceback.format_exc())
        await _update_run(pool, run_id, "failed", "error")
        await audit.log(run_id, "supervisor_error", {
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
