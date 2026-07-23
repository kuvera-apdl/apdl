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
from app.request_body_limit import RequestBodyLimitMiddleware
from app.routers import admin, evaluate, experiments, flags, stream
from app.schema import assert_schema_ready
from app.sse.broadcaster import SSEBroadcaster, SSESettings

logger = logging.getLogger(__name__)

CONFIG_LOCK_NAMESPACE = 0x4150444C
CONFIG_LOCK_ID = 0x434647
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084
MAINTENANCE_HEARTBEAT_SECONDS = 1.0
MAINTENANCE_HEARTBEAT_TIMEOUT_SECONDS = 2.0
EXPERIMENT_ANALYSIS_CAPABILITY_SCHEMA_VERSION = "config_experiment_analysis@1"


async def _acquire_maintenance_inhibitor(connection) -> None:
    """Block startup during maintenance and inhibit it while this connection lives."""
    await connection.execute(
        "SELECT pg_advisory_lock_shared($1)",
        MAINTENANCE_INHIBITOR_LOCK_ID,
    )
    await connection.execute(
        "SELECT pg_advisory_lock_shared($1)",
        MAINTENANCE_GUARD_LOCK_ID,
    )


async def _reset_maintenance_inhibitor(connection) -> None:
    """Apply asyncpg's default reset, then restore the session inhibitor."""
    reset_query = connection.get_reset_query()
    if reset_query:
        await connection.execute(reset_query)
    await _acquire_maintenance_inhibitor(connection)


async def _assert_maintenance_inhibitor_held(
    connection,
    *,
    heartbeat_timeout_seconds: float = MAINTENANCE_HEARTBEAT_TIMEOUT_SECONDS,
) -> None:
    held = await asyncio.wait_for(
        connection.fetchval(
            "SELECT count(*) = 2 FROM pg_catalog.pg_locks "
            "WHERE pid = pg_backend_pid() AND locktype = 'advisory' "
            "AND mode = 'ShareLock' AND granted "
            "AND classid = 0 AND objsubid = 1 "
            "AND objid IN ($1::bigint::oid, $2::bigint::oid)",
            MAINTENANCE_INHIBITOR_LOCK_ID,
            MAINTENANCE_GUARD_LOCK_ID,
        ),
        timeout=heartbeat_timeout_seconds,
    )
    if held is not True:
        raise RuntimeError("maintenance inhibitor locks were lost")


async def _monitor_maintenance_inhibitor(
    connection,
    connection_lost: asyncio.Event,
    *,
    heartbeat_seconds: float = MAINTENANCE_HEARTBEAT_SECONDS,
    heartbeat_timeout_seconds: float = MAINTENANCE_HEARTBEAT_TIMEOUT_SECONDS,
) -> None:
    """Terminate the process when its dedicated inhibitor session is uncertain."""
    while not connection_lost.is_set():
        try:
            await asyncio.wait_for(connection_lost.wait(), timeout=heartbeat_seconds)
        except TimeoutError:
            try:
                await _assert_maintenance_inhibitor_held(
                    connection,
                    heartbeat_timeout_seconds=heartbeat_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                connection_lost.set()
    logger.critical("PostgreSQL maintenance inhibitor was lost; terminating service")
    _abort_process_on_maintenance_loss()


def _abort_process_on_maintenance_loss() -> None:
    """Immediately stop in-flight work after the database barrier is lost."""
    os._exit(1)


async def _start_maintenance_monitor(connection):
    loop = asyncio.get_running_loop()
    connection_lost = asyncio.Event()

    def mark_connection_lost(_connection) -> None:
        loop.call_soon_threadsafe(connection_lost.set)

    connection.add_termination_listener(mark_connection_lost)
    try:
        await _assert_maintenance_inhibitor_held(connection)
    except BaseException:
        connection.remove_termination_listener(mark_connection_lost)
        raise
    task = asyncio.create_task(
        _monitor_maintenance_inhibitor(connection, connection_lost),
        name="maintenance-inhibitor-monitor",
    )
    return task, mark_connection_lost


async def _close_maintenance_monitor(connection, task, listener) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    connection.remove_termination_listener(listener)
    await connection.close()


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

    pg_pool = await asyncpg.create_pool(
        dsn=pg_dsn,
        min_size=2,
        max_size=pg_pool_size,
        init=_acquire_maintenance_inhibitor,
        reset=_reset_maintenance_inhibitor,
        max_inactive_connection_lifetime=0,
    )
    logger.info("PostgreSQL connection pool initialized")
    maintenance_connection = None
    maintenance_task = None
    maintenance_listener = None
    lock_conn = None
    redis_client = None
    broadcaster = None
    outbox_task = None
    lifecycle_task = None
    try:
        maintenance_connection = await pg_pool.acquire()
        maintenance_task, maintenance_listener = await _start_maintenance_monitor(
            maintenance_connection
        )
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

        if maintenance_task is not None:
            await _close_maintenance_monitor(
                maintenance_connection,
                maintenance_task,
                maintenance_listener,
            )
        elif maintenance_connection is not None:
            await maintenance_connection.close()
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
        credential_check_interval_seconds=_positive_float_environment(
            "SSE_CREDENTIAL_CHECK_INTERVAL_SECONDS",
            5.0,
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
app.add_middleware(RequestBodyLimitMiddleware)

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


@app.get("/ready/experiment-analysis")
async def experiment_analysis_capability_check(request: Request):
    """Prove the exact Config analysis contract and its backing schema."""
    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await assert_schema_ready(conn)
    except Exception as exc:
        logger.error(
            "Experiment-analysis capability check failed: %s",
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "apdl-config",
                "capability": "experiment_analysis",
                "schema_version": EXPERIMENT_ANALYSIS_CAPABILITY_SCHEMA_VERSION,
            },
        )
    return {
        "status": "ready",
        "service": "apdl-config",
        "capability": "experiment_analysis",
        "schema_version": EXPERIMENT_ANALYSIS_CAPABILITY_SCHEMA_VERSION,
    }


@app.get("/ready")
async def readiness_check(request: Request):
    """Return non-2xx while a required dependency cannot serve traffic."""
    checks = {
        "postgres": "not_ready",
        "redis": "not_ready",
        "outbox": "not_ready",
    }
    outbox_readiness = outbox.readiness_snapshot(outbox.empty_metrics())

    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            checks["postgres"] = "ready"
            outbox_metrics = await outbox.metrics_snapshot(conn)
        outbox_readiness = outbox.readiness_snapshot(outbox_metrics)
        checks["outbox"] = outbox_readiness["status"]
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
        "outbox": outbox_readiness,
        "sse": await request.app.state.broadcaster.metrics_snapshot(),
    }
    if any(value != "ready" for value in checks.values()):
        payload["status"] = "not_ready"
        return JSONResponse(status_code=503, content=payload)
    return payload
