"""Supervisor orchestration — registry-driven.

The supervisor no longer hard-codes each agent. It resolves the requested
agents from the framework registry, runs them in declared pipeline order,
skips any whose data dependencies (``requires``) are unmet, and threads a
shared state dict between them so each agent's ``produces`` output is visible
to the next. Run-status updates and audit logging are handled here, uniformly,
for every agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from typing import Any

import asyncpg

import app.graphs  # noqa: F401  ensures all agents are registered
from app.framework import AgentContext, BaseAgent, CustomAgent, get_agent, is_registered
from app.memory.pgvector_store import PgVectorStore
from app.safety.audit import AuditLogger
from app.store.custom_agents import fetch_active_by_slugs
from app.store.run_leases import (
    RunLeaseLostError,
    acquire_run_lease,
    maintain_run_lease,
    new_lease_owner_id,
    run_while_lease_owned,
)

logger = logging.getLogger(__name__)


class RunResultPersistenceError(RuntimeError):
    """A phase result could not be durably recorded for safe resume."""


@dataclass(frozen=True)
class PriorResults:
    completed_agents: frozenset[str]
    pending_gate_agent: str | None = None


async def _update_run(
    pool: asyncpg.Pool,
    run_id: str,
    status: str,
    phase: str,
    lease_owner_id: str,
    insights_count: int = 0,
    experiments_count: int = 0,
) -> None:
    """Update progress only while this supervisor owns an unexpired lease."""
    async with pool.acquire() as conn:
        updated = await conn.execute(
            """
            UPDATE agent_runs
            SET status = $2, phase = $3, insights_count = $4,
                experiments_count = $5, updated_at = now(),
                lease_owner_id = CASE WHEN $2 = 'running' THEN lease_owner_id ELSE NULL END,
                lease_expires_at = CASE WHEN $2 = 'running' THEN lease_expires_at ELSE NULL END
            WHERE run_id = $1
              AND execution_lane_project_id = project_id
              AND lease_owner_id = $6
              AND lease_expires_at > now()
            """,
            run_id,
            status,
            phase,
            insights_count,
            experiments_count,
            lease_owner_id,
        )
    if isinstance(updated, str) and updated.endswith(" 0"):
        raise RunLeaseLostError(f"Run {run_id} is no longer owned by {lease_owner_id}")


async def _persist_results(
    pool: asyncpg.Pool,
    run_id: str,
    agent_name: str,
    produces: str,
    output: Any,
    metadata: dict[str, Any],
    lease_owner_id: str,
) -> None:
    """Persist output and gate metadata before any later state transition."""
    durable_metadata = dict(metadata)
    if durable_metadata.get("needs_approval"):
        durable_metadata["approval_gate"] = {
            "gate_id": f"{run_id}:{agent_name}",
            "agent_name": agent_name,
            "produces": produces,
            "phase": f"{agent_name}_approval",
            "state": "pending",
        }
    try:
        async with pool.acquire() as conn:
            inserted = await conn.execute(
                """
                WITH owned_run AS (
                    SELECT run_id
                    FROM agent_runs
                    WHERE run_id = $1
                      AND execution_lane_project_id = project_id
                      AND lease_owner_id = $6
                      AND lease_expires_at > now()
                    FOR UPDATE
                )
                INSERT INTO agent_run_results
                    (run_id, agent_name, produces, output, metadata)
                SELECT $1, $2, $3, $4::jsonb, $5::jsonb
                FROM owned_run
                ON CONFLICT (run_id, agent_name)
                DO UPDATE SET produces = EXCLUDED.produces,
                              output = EXCLUDED.output,
                              metadata = EXCLUDED.metadata,
                              created_at = now()
                """,
                run_id,
                agent_name,
                produces,
                json.dumps(output if isinstance(output, list) else [output], default=str),
                json.dumps(durable_metadata, default=str),
                lease_owner_id,
            )
        if isinstance(inserted, str) and inserted.endswith(" 0"):
            raise RunLeaseLostError(
                f"Run {run_id} is no longer owned by {lease_owner_id}"
            )
    except RunLeaseLostError:
        raise
    except Exception as exc:
        raise RunResultPersistenceError(
            f"Could not persist {agent_name} result for run {run_id}"
        ) from exc


async def _load_prior_results(
    pool: asyncpg.Pool, run_id: str, state: dict[str, Any]
) -> PriorResults:
    """Reload a run's persisted agent outputs into ``state`` for a resume.

    Returns the set of agent names that already completed, so the supervisor can
    skip them and continue with the not-yet-run agents after an approval.
    """
    completed: set[str] = set()
    pending_gates: list[str] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_name, produces, output, metadata
            FROM agent_run_results
            WHERE run_id = $1
            ORDER BY created_at, agent_name
            """,
            run_id,
        )
    for row in rows:
        agent_name = str(row["agent_name"])
        completed.add(agent_name)
        output = row["output"]
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RunResultPersistenceError(
                    f"Persisted output for {run_id}/{agent_name} is malformed"
                ) from exc
        if not isinstance(output, list):
            raise RunResultPersistenceError(
                f"Persisted output for {run_id}/{agent_name} must be an array"
            )
        state[row["produces"]] = output if output is not None else []

        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RunResultPersistenceError(
                    f"Persisted metadata for {run_id}/{agent_name} is malformed"
                ) from exc
        if not isinstance(metadata, dict):
            raise RunResultPersistenceError(
                f"Persisted metadata for {run_id}/{agent_name} must be an object"
            )
        gate = metadata.get("approval_gate")
        if isinstance(gate, dict) and gate.get("state") == "pending":
            pending_gates.append(agent_name)

    if len(pending_gates) > 1:
        raise RunResultPersistenceError(
            f"Run {run_id} has multiple pending approval gates: {pending_gates}"
        )
    return PriorResults(
        completed_agents=frozenset(completed),
        pending_gate_agent=pending_gates[0] if pending_gates else None,
    )


async def _resolve_agents(
    pool: asyncpg.Pool,
    run_id: str,
    project_id: str,
    analysis_types: list[str],
    errors: list[str],
) -> list[BaseAgent]:
    """Resolve requested names to agent instances, in pipeline order.

    Built-ins (registry) always win; remaining names resolve against the
    project's active custom agents — hydrated from the DB at execution time,
    so an edit between trigger (or gate) and run uses the latest definition,
    and an archive turns the name into a skipped-with-error entry. Duplicates
    run once — a repeated name would double LLM spend and deploy attempts.
    """
    names: list[str] = []
    seen: set[str] = set()
    for name in analysis_types:
        if name not in seen:
            seen.add(name)
            names.append(name)

    custom_needed = [name for name in names if not is_registered(name)]
    custom_defs = (
        await fetch_active_by_slugs(pool, project_id, custom_needed) if custom_needed else {}
    )

    agents: list[BaseAgent] = []
    for name in names:
        if is_registered(name):
            agent = get_agent(name)
            if not getattr(agent, "enabled", True):
                # Older run configs (schedules, resumes) may still name a
                # since-disabled agent; skip it visibly instead of running it.
                msg = f"Agent '{name}' is disabled — skipping."
                logger.warning("[%s] %s", run_id, msg)
                errors.append(msg)
                continue
            agents.append(agent)
        elif name in custom_defs:
            agents.append(CustomAgent(custom_defs[name]))
        else:
            msg = f"Unknown agent '{name}' requested — skipping."
            logger.warning("[%s] %s", run_id, msg)
            errors.append(msg)
    agents.sort(key=lambda a: a.order)
    return agents


async def run_supervisor(
    pool: asyncpg.Pool,
    vector_store: PgVectorStore,
    run_id: str,
    project_id: str,
    analysis_types: list[str],
    time_range_days: int,
    autonomy_level: int,
    resume: bool = False,
    resume_after_approval: bool = False,
    target_proposal_id: str | None = None,
) -> None:
    """Acquire exclusive run ownership and execute the supervisor graph."""
    lease_owner_id = new_lease_owner_id()
    lease_claim_started = asyncio.get_running_loop().time()
    try:
        acquired = await acquire_run_lease(pool, run_id, lease_owner_id)
    except Exception:
        logger.exception("[%s] Could not acquire agent run lease", run_id)
        return

    if not acquired:
        logger.info("[%s] Run is already owned by another supervisor", run_id)
        return

    lease_stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        maintain_run_lease(
            pool,
            run_id,
            lease_owner_id,
            lease_stop,
            lease_lost,
            confirmed_at=lease_claim_started,
        )
    )
    try:
        await _run_owned_supervisor(
            pool=pool,
            vector_store=vector_store,
            run_id=run_id,
            project_id=project_id,
            analysis_types=analysis_types,
            time_range_days=time_range_days,
            autonomy_level=autonomy_level,
            lease_owner_id=lease_owner_id,
            lease_lost=lease_lost,
            resume=resume,
            resume_after_approval=resume_after_approval,
            target_proposal_id=target_proposal_id,
        )
    finally:
        lease_stop.set()
        await heartbeat_task
        # Normal terminal/waiting transitions clear ownership in _update_run.
        # An abnormal active exit deliberately retains owner + expiry so the
        # grace-delayed reaper can recover it; clearing here would turn it into
        # a 24-hour legacy orphan with no immediate recovery signal.


async def _run_owned_supervisor(
    pool: asyncpg.Pool,
    vector_store: PgVectorStore,
    run_id: str,
    project_id: str,
    analysis_types: list[str],
    time_range_days: int,
    autonomy_level: int,
    lease_owner_id: str,
    lease_lost: asyncio.Event,
    resume: bool = False,
    resume_after_approval: bool = False,
    target_proposal_id: str | None = None,
) -> None:
    """Execute one supervisor graph while holding an unexpired lease.

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
        lease_owner_id=lease_owner_id,
        autonomy_level=autonomy_level,
        time_range_days=time_range_days,
        target_proposal_id=target_proposal_id,
    )
    state: dict[str, Any] = {
        "project_id": project_id,
        "insights": [],
        "experiment_designs": [],
        "experiment_verdicts": [],
        "experiment_evidence_summaries": [],
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
        if lease_lost.is_set():
            raise RunLeaseLostError(f"Run {run_id} lease expired before execution")

        completed: set[str] = set()
        if resume:
            prior = await _load_prior_results(pool, run_id, state)
            completed = set(prior.completed_agents)
            await audit.log(run_id, "supervisor_resume", {
                "analysis_types": analysis_types,
                "completed": sorted(completed),
            })
            if prior.pending_gate_agent and not resume_after_approval:
                await _update_run(
                    pool,
                    run_id,
                    "waiting_approval",
                    f"{prior.pending_gate_agent}_approval",
                    lease_owner_id,
                    **_counts(),
                )
                await audit.log(
                    run_id,
                    "approval_gate_restored",
                    {"agent": prior.pending_gate_agent},
                )
                logger.info(
                    "[%s] Restored pending approval gate for %s",
                    run_id,
                    prior.pending_gate_agent,
                )
                return
        else:
            await audit.log(run_id, "supervisor_start", {
                "analysis_types": analysis_types,
                "autonomy_level": autonomy_level,
                "time_range_days": time_range_days,
            })

        agents = await _resolve_agents(
            pool, run_id, project_id, analysis_types, state["errors"]
        )

        for agent in agents:
            if lease_lost.is_set():
                raise RunLeaseLostError(f"Run {run_id} lease expired")
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

            await _update_run(
                pool,
                run_id,
                "running",
                agent.name,
                lease_owner_id,
                **_counts(),
            )
            logger.info("[%s] Running agent: %s", run_id, agent.name)

            try:
                result = await run_while_lease_owned(
                    agent.run(ctx, state),
                    lease_lost,
                    run_id=run_id,
                )
                if lease_lost.is_set():
                    raise RunLeaseLostError(f"Run {run_id} lease expired")
                state[agent.produces] = result.output
                await _persist_results(
                    pool,
                    run_id,
                    agent.name,
                    agent.produces,
                    result.output,
                    result.metadata,
                    lease_owner_id,
                )
                if lease_lost.is_set():
                    raise RunLeaseLostError(f"Run {run_id} lease expired")

                post_persist = getattr(agent, "after_result_persisted", None)
                if post_persist is not None:
                    await run_while_lease_owned(
                        post_persist(ctx, state, result),
                        lease_lost,
                        run_id=run_id,
                    )
                if lease_lost.is_set():
                    raise RunLeaseLostError(f"Run {run_id} lease expired")

                await audit.log(run_id, f"{agent.name}_complete", {
                    "produced": agent.produces,
                    "count": len(result.output) if isinstance(result.output, list) else 1,
                    **result.metadata,
                })

                if result.metadata.get("needs_approval"):
                    await _update_run(
                        pool,
                        run_id,
                        "waiting_approval",
                        f"{agent.name}_approval",
                        lease_owner_id,
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
            except (RunLeaseLostError, RunResultPersistenceError):
                raise
            except Exception as exc:
                error_msg = f"{agent.name} failed: {exc}"
                logger.error("[%s] %s", run_id, error_msg)
                state["errors"].append(error_msg)
                await audit.log(run_id, f"{agent.name}_error", {"error": str(exc)})

        final_status = "completed" if not state["errors"] else "completed_with_errors"
        await _update_run(
            pool,
            run_id,
            final_status,
            "done",
            lease_owner_id,
            **_counts(),
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

    except RunLeaseLostError:
        # Another worker/reaper now owns the state transition. This stale task
        # must not overwrite it or emit an audit entry claiming failure.
        logger.warning("[%s] Supervisor stopped after losing its lease", run_id)
    except RunResultPersistenceError:
        # Keep the active lease in place. Its expiry will requeue the same run,
        # and completed phases are reconstructed from durable results.
        logger.exception("[%s] Supervisor stopped on durable result failure", run_id)
    except Exception as exc:
        logger.error("[%s] Supervisor failed: %s\n%s", run_id, exc, traceback.format_exc())
        # The failure path itself must be crash-proof (the original error is
        # often DB-related, so these updates can fail too) and must not zero
        # out real progress counters.
        try:
            await _update_run(
                pool,
                run_id,
                "failed",
                "error",
                lease_owner_id,
                **_counts(),
            )
        except Exception:
            logger.exception("[%s] Could not mark run failed", run_id)
        try:
            await audit.log(run_id, "supervisor_error", {
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })
        except Exception:
            logger.exception("[%s] Could not audit supervisor error", run_id)
