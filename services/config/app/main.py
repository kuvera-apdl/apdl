"""APDL Config Service -- FastAPI application entry point."""

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import outbox
from app.auth import (
    AuthIdentity,
    PostgresAuthenticator,
    Principal,
    authenticate_request,
)
from app.client_ip import parse_trusted_proxy_cidrs
from app.experiments import lifecycle
from app.routers import admin, evaluate, experiments, flags, stream
from app.schema import assert_schema_ready
from app.sse.broadcaster import SSEBroadcaster, SSESettings

logger = logging.getLogger(__name__)

CONFIG_LOCK_NAMESPACE = 0x4150444C
CONFIG_LOCK_ID = 0x434647


async def _acquire_config_lock(pool):
    """Hold one PostgreSQL session lock for the Config process lifetime."""
    conn = await pool.acquire()
    acquired = await conn.fetchval(
        "SELECT pg_try_advisory_lock($1, $2)",
        CONFIG_LOCK_NAMESPACE,
        CONFIG_LOCK_ID,
    )
    if acquired:
        return conn
    await pool.release(conn)
    raise RuntimeError(
        "Another Config process holds the OSS single-replica database lock"
    )


async def _release_config_lock(pool, conn) -> None:
    await conn.fetchval(
        "SELECT pg_advisory_unlock($1, $2)",
        CONFIG_LOCK_NAMESPACE,
        CONFIG_LOCK_ID,
    )
    await pool.release(conn)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    sse_settings = _sse_settings_from_environment()
    trusted_proxy_networks = parse_trusted_proxy_cidrs(
        os.environ.get("CONFIG_TRUSTED_PROXY_CIDRS", "")
    )
    pg_dsn = os.environ.get(
        "POSTGRES_URL",
        "postgresql://apdl:apdl_dev@localhost:5432/apdl",
    )
    pg_pool_size = int(os.environ.get("PG_POOL_SIZE", "4"))

    pg_pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=pg_pool_size)
    logger.info("PostgreSQL connection pool initialized")
    lock_conn = None
    redis_client = None
    broadcaster = None
    outbox_task = None
    lifecycle_task = None
    try:
        lock_conn = await _acquire_config_lock(pg_pool)
        logger.info("Config single-replica database lock acquired")

        # Schema authority belongs to pipeline/postgres/migrations. The app
        # validates it and fails closed instead of racing startup DDL.
        await assert_schema_ready(lock_conn)
        logger.info("Database schema migration verified")

        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        redis_client = aioredis.from_url(redis_url)
        logger.info("Redis connection initialized")

        broadcaster = SSEBroadcaster(sse_settings)
        await broadcaster.start()
        logger.info("SSE broadcaster started")

        outbox_task = asyncio.create_task(
            outbox.run_worker(pg_pool, redis_client, broadcaster)
        )
        logger.info("Config durable outbox worker started")

        lifecycle_task = _start_lifecycle_monitor(pg_pool)

        application.state.pg_pool = pg_pool
        application.state.authenticator = PostgresAuthenticator(pg_pool)
        application.state.redis = redis_client
        application.state.broadcaster = broadcaster
        application.state.trusted_proxy_networks = trusted_proxy_networks
        application.state.outbox_task = outbox_task
        application.state.lifecycle_task = lifecycle_task

        yield
    finally:
        if lifecycle_task is not None:
            lifecycle_task.cancel()
            try:
                await lifecycle_task
            except asyncio.CancelledError:
                pass
            logger.info("Experiment lifecycle monitor stopped")

        if outbox_task is not None:
            outbox_task.cancel()
            try:
                await outbox_task
            except asyncio.CancelledError:
                pass
            logger.info("Config durable outbox worker stopped")

        if broadcaster is not None:
            await broadcaster.stop()
            logger.info("SSE broadcaster stopped")

        if redis_client is not None:
            await redis_client.aclose()
            logger.info("Redis connection closed")

        if lock_conn is not None:
            await _release_config_lock(pg_pool, lock_conn)
            logger.info("Config single-replica database lock released")

        await pg_pool.close()
        logger.info("PostgreSQL connection pool closed")


def _positive_int_environment(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float_environment(name: str, default: float) -> float:
    raw_value = os.environ.get(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive duration") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive duration")
    return value


def _sse_settings_from_environment() -> SSESettings:
    return SSESettings(
        queue_capacity=_positive_int_environment("SSE_QUEUE_CAPACITY", 256),
        max_connections=_positive_int_environment("SSE_MAX_CONNECTIONS", 1000),
        max_connections_per_project=_positive_int_environment(
            "SSE_MAX_CONNECTIONS_PER_PROJECT",
            100,
        ),
        max_connections_per_credential=_positive_int_environment(
            "SSE_MAX_CONNECTIONS_PER_CREDENTIAL",
            10,
        ),
        max_connections_per_ip=_positive_int_environment(
            "SSE_MAX_CONNECTIONS_PER_IP",
            20,
        ),
        ping_interval_seconds=_positive_float_environment(
            "SSE_PING_INTERVAL_SECONDS",
            15.0,
        ),
        send_timeout_seconds=_positive_float_environment(
            "SSE_SEND_TIMEOUT_SECONDS",
            10.0,
        ),
        max_lifetime_seconds=_positive_float_environment(
            "SSE_MAX_LIFETIME_SECONDS",
            300.0,
        ),
    )


def _start_lifecycle_monitor(pg_pool) -> asyncio.Task | None:
    if os.environ.get("EXPERIMENT_LIFECYCLE_ENABLED", "true").lower() != "true":
        logger.info("Experiment lifecycle monitor disabled")
        return None
    raw_interval = os.environ.get("EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS", "300")
    try:
        interval_seconds = int(raw_interval)
    except ValueError as exc:
        raise ValueError(
            "EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS must be an integer"
        ) from exc
    lifecycle.validate_interval_seconds(interval_seconds)
    logger.info("Starting experiment lifecycle monitor every %ds", interval_seconds)
    return asyncio.create_task(
        lifecycle.run_lifecycle_monitor(
            pg_pool,
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
app.include_router(experiments.router, dependencies=auth_dependencies)
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
    """Process liveness does not depend on downstream availability."""
    return {"status": "ok", "service": "apdl-config"}


@app.get("/ready")
async def readiness_check(request: Request):
    """Return non-2xx while a required dependency cannot serve traffic."""
    checks = {"postgres": "not_ready", "redis": "not_ready"}

    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ready"
    except Exception as exc:
        logger.error("Readiness check: PostgreSQL error: %s", exc)

    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ready"
    except Exception as exc:
        logger.error("Readiness check: Redis error: %s", exc)

    payload = {
        "status": "ready",
        "service": "apdl-config",
        "checks": checks,
        "sse": await request.app.state.broadcaster.metrics_snapshot(),
    }
    if any(value != "ready" for value in checks.values()):
        payload["status"] = "not_ready"
        return JSONResponse(status_code=503, content=payload)
    return payload
