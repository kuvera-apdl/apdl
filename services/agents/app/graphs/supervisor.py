"""Supervisor orchestration — registry-driven.

The supervisor no longer hard-codes each agent. It resolves the requested
agents from the framework registry, runs them in declared pipeline order,
skips any whose data dependencies (``requires``) are unmet, and threads a
shared state dict between them so each agent's ``produces`` output is visible
to the next. Run-status updates and audit logging are handled here, uniformly,
for every agent.
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

import asyncpg

import app.graphs  # noqa: F401  ensures all agents are registered
from app.framework import AgentContext, get_agent, is_registered
from app.memory.pgvector_store import PgVectorStore
from app.safety.audit import AuditLogger

logger = logging.getLogger(__name__)


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


async def _persist_results(
    pool: asyncpg.Pool,
    run_id: str,
    agent_name: str,
    produces: str,
    output: Any,
) -> None:
    """Persist an agent's output at phase completion (admin-plan gap G3).

    Best-effort: persistence failures are logged and must never kill a run.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO agent_run_results (run_id, agent_name, produces, output)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (run_id, agent_name)
                DO UPDATE SET output = EXCLUDED.output, created_at = now()
                """,
                run_id,
                agent_name,
                produces,
                json.dumps(output if isinstance(output, list) else [output], default=str),
            )
    except Exception:
        logger.exception("[%s] Failed to persist %s results", run_id, agent_name)


async def _load_prior_results(
    pool: asyncpg.Pool, run_id: str, state: dict[str, Any]
) -> set[str]:
    """Reload a run's persisted agent outputs into ``state`` for a resume.

    Returns the set of agent names that already completed, so the supervisor can
    skip them and continue with the not-yet-run agents after an approval.
    """
    completed: set[str] = set()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_name, produces, output FROM agent_run_results WHERE run_id = $1",
            run_id,
        )
    for row in rows:
        completed.add(row["agent_name"])
        output = row["output"]
        if isinstance(output, str):
            # A malformed row must not kill the resume — the run would wedge
            # at 'resuming' with no way to re-kick it (same defense as the
            # approval gate's _load_gate_items).
            try:
                output = json.loads(output)
            except (json.JSONDecodeError, ValueError):
                logger.error(
                    "[%s] Skipping malformed persisted output for %s", run_id, row["agent_name"]
                )
                state.setdefault(row["produces"], [])
                continue
        state[row["produces"]] = output if output is not None else []
    return completed


async def run_supervisor(
    pool: asyncpg.Pool,
    vector_store: PgVectorStore,
    run_id: str,
    project_id: str,
    analysis_types: list[str],
    time_range_days: int,
    autonomy_level: int,
    resume: bool = False,
    target_proposal_id: str | None = None,
) -> None:
    """Execute the supervisor orchestration as a background task.

    Resolves the requested ``analysis_types`` against the agent registry, runs
    each in pipeline order, and passes outputs forward via shared state.

    ``target_proposal_id`` scopes a forked ``code_implementation`` run to a
    single approved proposal (one PR per proposal). On ``resume``, agents
    already present in the persisted results are skipped, so the just-approved
    gated agent never re-runs (and never re-gates) — only not-yet-run downstream
    agents execute, or the run finalizes to ``done`` when none remain.
    """
    audit = AuditLogger(pool)
    ctx = AgentContext(
        pool=pool,
        vector_store=vector_store,
        audit=audit,
        run_id=run_id,
        project_id=project_id,
        autonomy_level=autonomy_level,
        time_range_days=time_range_days,
        target_proposal_id=target_proposal_id,
    )
    state: dict[str, Any] = {
        "project_id": project_id,
        "insights": [],
        "experiment_designs": [],
        "personalizations": [],
        "feature_proposals": [],
        "errors": [],
    }

    def _counts() -> dict[str, int]:
        return {
            "insights_count": len(state["insights"]),
            "experiments_count": len(state["experiment_designs"]),
        }

    # Everything — including resume initialization — runs inside the try: a
    # transient DB error while reloading prior results used to kill the task
    # before any status update, wedging the run at phase 'resuming' forever.
    try:
        completed: set[str] = set()
        if resume:
            completed = await _load_prior_results(pool, run_id, state)
            await audit.log(run_id, "supervisor_resume", {
                "analysis_types": analysis_types,
                "completed": sorted(completed),
            })
        else:
            await audit.log(run_id, "supervisor_start", {
                "analysis_types": analysis_types,
                "autonomy_level": autonomy_level,
                "time_range_days": time_range_days,
            })

        # Resolve requested agents, warn on unknown names, order by pipeline
        # order. Duplicates run once — a repeated name would double LLM spend
        # and double deploy attempts.
        agents = []
        seen: set[str] = set()
        for name in analysis_types:
            if name in seen:
                continue
            seen.add(name)
            if not is_registered(name):
                msg = f"Unknown agent '{name}' requested — skipping."
                logger.warning("[%s] %s", run_id, msg)
                state["errors"].append(msg)
                continue
            agents.append(get_agent(name))
        agents.sort(key=lambda a: a.order)

        for agent in agents:
            if agent.name in completed:
                logger.info("[%s] Skipping %s — already completed (resume)", run_id, agent.name)
                continue
            if not agent.requirements_met(state):
                logger.info(
                    "[%s] Skipping %s — unmet requirements %s",
                    run_id, agent.name, agent.requires,
                )
                await audit.log(run_id, f"{agent.name}_skipped", {
                    "reason": "unmet_requirements",
                    "requires": list(agent.requires),
                })
                continue

            await _update_run(pool, run_id, "running", agent.name, **_counts())
            logger.info("[%s] Running agent: %s", run_id, agent.name)

            try:
                result = await agent.run(ctx, state)
                state[agent.produces] = result.output
                await _persist_results(pool, run_id, agent.name, agent.produces, result.output)

                await audit.log(run_id, f"{agent.name}_complete", {
                    "produced": agent.produces,
                    "count": len(result.output) if isinstance(result.output, list) else 1,
                    **result.metadata,
                })

                if result.metadata.get("needs_approval"):
                    await _update_run(
                        pool, run_id, "waiting_approval", f"{agent.name}_approval",
                        **_counts(),
                    )
                    await audit.log(run_id, "waiting_approval", {
                        "agent": agent.name,
                        "experiment_id": result.metadata.get("experiment_id", ""),
                    })
                    logger.info("[%s] %s awaiting human approval", run_id, agent.name)
                    # Halt the pipeline at the gate: the run stays in
                    # waiting_approval until a human decides (approval then
                    # deploys the gated action — see routers/approvals.py).
                    # Without this return the loop would fall through to the
                    # unconditional "completed" below and the gate would be lost.
                    return
            except Exception as exc:
                error_msg = f"{agent.name} failed: {exc}"
                logger.error("[%s] %s", run_id, error_msg)
                state["errors"].append(error_msg)
                await audit.log(run_id, f"{agent.name}_error", {"error": str(exc)})

        final_status = "completed" if not state["errors"] else "completed_with_errors"
        await _update_run(pool, run_id, final_status, "done", **_counts())

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
        # The failure path itself must be crash-proof (the original error is
        # often DB-related, so these updates can fail too) and must not zero
        # out real progress counters.
        try:
            await _update_run(pool, run_id, "failed", "error", **_counts())
        except Exception:
            logger.exception("[%s] Could not mark run failed", run_id)
        try:
            await audit.log(run_id, "supervisor_error", {
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
        except Exception:
            logger.exception("[%s] Could not audit supervisor error", run_id)
