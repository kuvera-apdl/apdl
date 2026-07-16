"""APDL Agents Service — FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import PostgresAuthenticator, authenticate_request
from app.memory.pgvector_store import PgVectorStore
from app.routers import approvals, custom_agents, runs, status, triggers
from app.schema import assert_schema_ready
from app.store.run_leases import (
    reap_abandoned_runs_forever,
    recover_abandoned_runs,
)

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
    recovered = await recover_abandoned_runs(pool)
    if recovered.abandoned_run_ids:
        logger.warning(
            "Startup lease recovery marked %d abandoned run(s) failed",
            len(recovered.abandoned_run_ids),
        )
    if recovered.reopened_proposal_ids:
        logger.warning(
            "Startup lease recovery reopened %d abandoned proposal claim(s)",
            len(recovered.reopened_proposal_ids),
        )

    reaper_stop = asyncio.Event()
    reaper_task = asyncio.create_task(reap_abandoned_runs_forever(pool, reaper_stop))

    vector_store = PgVectorStore(pool)
    application.state.vector_store = vector_store

    logger.info("Agents service started: PostgreSQL pool and vector store initialized")
    try:
        yield
    finally:
        reaper_stop.set()
        await reaper_task
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
async def readiness_check():
    """Readiness probe — verifies PostgreSQL connectivity."""
    try:
        pool: asyncpg.Pool = app.state.pg_pool
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready"}
    except Exception as exc:
        # 503 so LB/K8s probes (which key on the status code) stop routing
        # here; the raw exception stays in the logs — it can carry DSN details.
        logger.error("Readiness check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
