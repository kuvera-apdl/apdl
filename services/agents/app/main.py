"""APDL Agents Service — FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import PostgresAuthenticator, authenticate_request
from app.memory.pgvector_store import PgVectorStore
from app.readiness import capability_report
from app.routers import approvals, custom_agents, runs, status, triggers
from app.schema import assert_schema_ready
from app.store.approval_effects import run_approval_effect_worker_forever
from app.store.llm_governance import (
    reconcile_orphaned_llm_attempts,
    reconcile_orphaned_llm_attempts_forever,
)
from app.store.run_leases import (
    requeue_expired_runs,
    requeue_expired_runs_forever,
)
from app.store.run_dispatcher import dispatch_runs_forever

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    dsn = os.getenv("POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl")

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=20)
    application.state.pg_pool = pool
    application.state.authenticator = PostgresAuthenticator(pool)

    # Schema authority belongs to pipeline/postgres/migrations. Application
    # replicas validate it and fail closed instead of racing startup DDL.
    async with pool.acquire() as conn:
        await assert_schema_ready(conn)

    # Reconcile only expired ownership. Every replica may run this safely: an
    # unexpired lease belongs to a live task on this or another replica, and a
    # proposal is reopened only when it was claimed by the abandoned run.
    recovered = await requeue_expired_runs(pool)
    if recovered.requeued_run_ids:
        logger.warning(
            "Startup lease recovery requeued %d expired run(s)",
            len(recovered.requeued_run_ids),
        )
    if recovered.reopened_proposal_ids:
        logger.warning(
            "Startup lease recovery reopened %d abandoned proposal claim(s)",
            len(recovered.reopened_proposal_ids),
        )
    reconciled_llm = await reconcile_orphaned_llm_attempts(pool)
    if reconciled_llm.prepared_blocked or reconciled_llm.in_flight_cancelled:
        logger.warning(
            "Startup recovery reconciled %d pre-egress and %d unknown-outcome "
            "LLM attempt(s)",
            reconciled_llm.prepared_blocked,
            reconciled_llm.in_flight_cancelled,
        )

    vector_store = PgVectorStore(pool)
    application.state.vector_store = vector_store

    worker_stop = asyncio.Event()
    reaper_task = asyncio.create_task(
        requeue_expired_runs_forever(pool, worker_stop),
        name="agent-run-requeue",
    )
    dispatcher_task = asyncio.create_task(
        dispatch_runs_forever(pool, vector_store, worker_stop),
        name="agent-run-dispatcher",
    )
    approval_effect_task = asyncio.create_task(
        run_approval_effect_worker_forever(pool, worker_stop),
        name="agent-approval-effects",
    )
    llm_reconciliation_task = asyncio.create_task(
        reconcile_orphaned_llm_attempts_forever(pool, worker_stop),
        name="agent-llm-attempt-reconciliation",
    )
    application.state.run_reaper_task = reaper_task
    application.state.run_dispatcher_task = dispatcher_task
    application.state.approval_effect_task = approval_effect_task
    application.state.llm_reconciliation_task = llm_reconciliation_task

    logger.info("Agents service started: PostgreSQL pool and vector store initialized")
    try:
        yield
    finally:
        worker_stop.set()
        await asyncio.gather(
            dispatcher_task,
            reaper_task,
            approval_effect_task,
            llm_reconciliation_task,
        )
        await pool.close()
        logger.info("Agents service shut down: PostgreSQL pool closed")


app = FastAPI(
    title="APDL Agents Service",
    version="0.1.0",
    lifespan=lifespan,
)

# No cookie/session auth is used, so credentialed CORS is never needed — and
# combining allow_credentials with a wildcard origin makes Starlette echo any
# Origin back, letting arbitrary websites make credentialed requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# custom_agents first: it owns static shapes (/custom/*, /definitions) that
# must win over the run routers' /{run_id}/... wildcards.
auth_dependencies = [Depends(authenticate_request)]
app.include_router(custom_agents.router, dependencies=auth_dependencies)
app.include_router(triggers.router, dependencies=auth_dependencies)
app.include_router(status.router, dependencies=auth_dependencies)
app.include_router(approvals.router, dependencies=auth_dependencies)
app.include_router(runs.router, dependencies=auth_dependencies)


@app.get("/health")
async def health_check():
    """Liveness probe."""
    return {"status": "ok", "service": "apdl-agents"}


@app.get("/ready")
async def readiness_check(request: Request):
    """Core readiness: local runtime initialization and PostgreSQL only."""
    checks = {"runtime": "not_ready", "postgres": "not_ready"}
    state = request.app.state

    runtime_objects_ready = all(
        getattr(state, attribute, None) is not None
        for attribute in ("pg_pool", "authenticator", "vector_store")
    )
    runtime_tasks = (
        getattr(state, "run_dispatcher_task", None),
        getattr(state, "run_reaper_task", None),
        getattr(state, "approval_effect_task", None),
        getattr(state, "llm_reconciliation_task", None),
    )
    if runtime_objects_ready and all(
        task is not None and not task.done() for task in runtime_tasks
    ):
        checks["runtime"] = "ready"

    try:
        pool: asyncpg.Pool = state.pg_pool
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ready"
    except Exception as exc:
        logger.error("Core readiness PostgreSQL check failed: %s", type(exc).__name__)

    payload = {"status": "ready", "service": "apdl-agents", "checks": checks}
    if any(value != "ready" for value in checks.values()):
        payload["status"] = "not_ready"
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/ready/capabilities")
async def capability_readiness_check():
    """Report optional LLM and service capabilities without gating traffic."""
    return await capability_report()
