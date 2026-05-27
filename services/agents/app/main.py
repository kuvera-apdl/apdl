"""APDL Agents Service — FastAPI application entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.memory.pgvector_store import PgVectorStore
from app.routers import approvals, status, triggers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    dsn = os.getenv("POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl")

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=20)
    application.state.pg_pool = pool

    # Ensure required tables exist
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE EXTENSION IF NOT EXISTS vector;
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id BIGSERIAL PRIMARY KEY,
                project_id TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata JSONB DEFAULT '{}',
                embedding vector(1536),
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
            ON agent_memory USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100);
        """)
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
                updated_at TIMESTAMPTZ DEFAULT now()
            );
        """)
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

    vector_store = PgVectorStore(pool)
    application.state.vector_store = vector_store

    logger.info("Agents service started: PostgreSQL pool and vector store initialized")
    yield

    await pool.close()
    logger.info("Agents service shut down: PostgreSQL pool closed")


app = FastAPI(
    title="APDL Agents Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(triggers.router)
app.include_router(status.router)
app.include_router(approvals.router)


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
        logger.error("Readiness check failed: %s", exc)
        return {"status": "not_ready", "error": str(exc)}
