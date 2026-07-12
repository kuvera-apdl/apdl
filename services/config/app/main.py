"""APDL Config Service -- FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import (
    AuthIdentity,
    PostgresAuthenticator,
    Principal,
    authenticate_request,
)
from app.experiments import expiry
from app.routers import admin, evaluate, flags, stream
from app.schema import assert_schema_ready
from app.sse.broadcaster import SSEBroadcaster

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    pg_dsn = os.environ.get(
        "POSTGRES_URL",
        "postgresql://apdl:apdl_dev@localhost:5432/apdl",
    )
    pg_pool_size = int(os.environ.get("PG_POOL_SIZE", "4"))

    pg_pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=pg_pool_size)
    logger.info("PostgreSQL connection pool initialized")

    # Schema authority belongs to pipeline/postgres/migrations. Application
    # replicas validate it and fail closed instead of racing startup DDL.
    async with pg_pool.acquire() as conn:
        await assert_schema_ready(conn)
    logger.info("Database schema migration verified")

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_client = aioredis.from_url(redis_url)
    logger.info("Redis connection initialized")

    broadcaster = SSEBroadcaster()
    await broadcaster.start()
    logger.info("SSE broadcaster started")

    # Experiment expiry monitor: completes running experiments past their
    # end_date (cascading to disable their backing flags). Nothing else acts on
    # end_date, so without this they run forever.
    expiry_task = _start_expiry_monitor(pg_pool, redis_client, broadcaster)

    application.state.pg_pool = pg_pool
    application.state.authenticator = PostgresAuthenticator(pg_pool)
    application.state.redis = redis_client
    application.state.broadcaster = broadcaster
    application.state.expiry_task = expiry_task

    yield

    if expiry_task is not None:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        logger.info("Experiment expiry monitor stopped")

    await broadcaster.stop()
    logger.info("SSE broadcaster stopped")

    await redis_client.aclose()
    logger.info("Redis connection closed")

    await pg_pool.close()
    logger.info("PostgreSQL connection pool closed")


def _start_expiry_monitor(pg_pool, redis_client, broadcaster) -> asyncio.Task | None:
    if os.environ.get("EXPERIMENT_EXPIRY_ENABLED", "true").lower() != "true":
        logger.info("Experiment expiry monitor disabled")
        return None
    interval_seconds = int(os.environ.get("EXPERIMENT_EXPIRY_INTERVAL_SECONDS", "300"))
    logger.info("Starting experiment expiry monitor every %ds", interval_seconds)
    return asyncio.create_task(
        expiry.run_expiry_monitor(
            pg_pool,
            redis_client,
            broadcaster,
            interval_seconds=interval_seconds,
        )
    )


app = FastAPI(
    title="APDL Config Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

auth_dependencies = [Depends(authenticate_request)]
app.include_router(flags.router, dependencies=auth_dependencies)
app.include_router(stream.router, dependencies=auth_dependencies)
app.include_router(evaluate.router, dependencies=auth_dependencies)
app.include_router(admin.router, dependencies=auth_dependencies)


@app.get("/v1/auth/me", response_model=AuthIdentity)
async def authenticated_identity(
    principal: Principal = Depends(authenticate_request),
) -> AuthIdentity:
    """Return the project and roles attached to the verified API key."""
    return AuthIdentity(
        credential_id=principal.credential_id,
        project_id=principal.project_id,
        roles=sorted(principal.roles),
    )


@app.get("/health")
async def health_check():
    """Liveness/readiness probe -- checks PG, Redis, and SSE connection count."""
    status = {"status": "ok", "service": "apdl-config"}

    try:
        async with app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        status["postgres"] = "ok"
    except Exception as exc:
        logger.error("Health check: PostgreSQL error: %s", exc)
        status["postgres"] = "error"
        status["status"] = "degraded"

    try:
        await app.state.redis.ping()
        status["redis"] = "ok"
    except Exception as exc:
        logger.error("Health check: Redis error: %s", exc)
        status["redis"] = "error"
        status["status"] = "degraded"

    status["sse_connections"] = await app.state.broadcaster.total_connection_count()

    return status
