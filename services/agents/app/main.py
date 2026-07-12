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
from app.memory.embeddings import EMBEDDING_DIMENSIONS
from app.memory.pgvector_store import PgVectorStore
from app.routers import approvals, custom_agents, runs, status, triggers
from app.store.custom_agents import (
    CUSTOM_AGENTS_DDL,
    CUSTOM_AGENTS_INDEX_DDL,
    CUSTOM_AGENTS_MIGRATE_DDL,
)
from app.store.experiments import (
    DESIGNED_EXPERIMENTS_DDL,
    DESIGNED_EXPERIMENTS_INDEX_DDL,
    DESIGNED_EXPERIMENTS_MIGRATE_DDL,
)
from app.store.proposals import FEATURE_PROPOSALS_DDL, FEATURE_PROPOSALS_MIGRATE_DDL
from app.store.run_leases import (
    AGENT_RUN_LEASE_MIGRATE_DDL,
    reap_abandoned_runs_forever,
    recover_abandoned_runs,
)
from app.store.verdicts import (
    EXPERIMENT_VERDICTS_DDL,
    EXPERIMENT_VERDICTS_INDEX_DDL,
)

logger = logging.getLogger(__name__)


async def ensure_agent_memory_schema(conn) -> None:
    """Create agent_memory and reconcile the embedding column dimension.

    The table DDL is ``CREATE TABLE IF NOT EXISTS``, so on a DB that already
    booted an older build the embedding column keeps its previous dimension while
    embed() now emits EMBEDDING_DIMENSIONS-dim vectors — every store()/search()
    would then raise a dimension mismatch, silently swallowed by the supervisor.
    Detect a stale dimension and migrate in place: drop the ivfflat index, purge
    the now-incompatible rows (old vectors are from a different model, not just a
    different width), and ALTER the column to the current dimension. The index is
    (re)created afterward. Idempotent: a no-op once the column already matches.
    """
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    await conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS agent_memory (
            id BIGSERIAL PRIMARY KEY,
            project_id TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata JSONB DEFAULT '{{}}',
            embedding vector({EMBEDDING_DIMENSIONS}),
            created_at TIMESTAMPTZ DEFAULT now()
        );
        """
    )

    # pgvector stores the declared dimension in atttypmod (-1 if unspecified).
    current_dim = await conn.fetchval(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = 'agent_memory'::regclass AND attname = 'embedding' "
        "AND NOT attisdropped"
    )
    if current_dim is not None and current_dim not in (-1, EMBEDDING_DIMENSIONS):
        logger.warning(
            "agent_memory.embedding is vector(%s) but the model now emits %s-dim "
            "vectors; migrating in place and purging incompatible rows.",
            current_dim,
            EMBEDDING_DIMENSIONS,
        )
        await conn.execute("DROP INDEX IF EXISTS idx_agent_memory_embedding;")
        await conn.execute("DELETE FROM agent_memory;")
        await conn.execute(
            f"ALTER TABLE agent_memory ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS});"
        )

    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
        ON agent_memory USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
        """
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    dsn = os.getenv("POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl")

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=20)
    application.state.pg_pool = pool
    application.state.authenticator = PostgresAuthenticator(pool)

    # Ensure required tables exist
    async with pool.acquire() as conn:
        await ensure_agent_memory_schema(conn)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                autonomy_level INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'started',
                phase TEXT DEFAULT 'initializing',
                insights_count INTEGER DEFAULT 0,
                experiments_count INTEGER DEFAULT 0,
                config JSONB DEFAULT '{}',
                started_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                lease_owner_id TEXT,
                lease_expires_at TIMESTAMPTZ
            );
        """)
        await conn.execute(AGENT_RUN_LEASE_MIGRATE_DDL)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_audit_log (
                id BIGSERIAL PRIMARY KEY,
                run_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                config JSONB DEFAULT '{}',
                safety_result JSONB DEFAULT '{}',
                approval_status TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_run_results (
                run_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                produces TEXT NOT NULL,
                output JSONB DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (run_id, agent_name)
            );
        """)
        await conn.execute(FEATURE_PROPOSALS_DDL)
        await conn.execute(FEATURE_PROPOSALS_MIGRATE_DDL)
        await conn.execute(DESIGNED_EXPERIMENTS_DDL)
        await conn.execute(DESIGNED_EXPERIMENTS_INDEX_DDL)
        await conn.execute(DESIGNED_EXPERIMENTS_MIGRATE_DDL)
        await conn.execute(EXPERIMENT_VERDICTS_DDL)
        await conn.execute(EXPERIMENT_VERDICTS_INDEX_DDL)
        await conn.execute(CUSTOM_AGENTS_DDL)
        await conn.execute(CUSTOM_AGENTS_INDEX_DDL)
        await conn.execute(CUSTOM_AGENTS_MIGRATE_DDL)

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
