"""APDL Ingestion Service -- FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import PostgresAuthenticator, authenticate_request
from app.client_ip import parse_trusted_proxy_cidrs
from app.middleware.rate_limit import PreAuthRateLimitMiddleware
from app.request_body_limit import RequestBodyLimitMiddleware
from app.routers import events
from app.validation.json_contract import MAX_REQUEST_BYTES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084
MAINTENANCE_HEARTBEAT_SECONDS = 1.0
MAINTENANCE_HEARTBEAT_TIMEOUT_SECONDS = 2.0


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


@dataclass(frozen=True)
class RedisLogTarget:
    """Non-secret Redis connection metadata safe for application logs."""

    host: str
    port: int | None
    database: str
    tls: bool


def _redis_log_target(redis_url: str) -> RedisLogTarget:
    parsed = urlsplit(redis_url)
    if parsed.scheme == "unix":
        return RedisLogTarget(
            host="unix-socket",
            port=None,
            database="0",
            tls=False,
        )
    path_database = parsed.path.removeprefix("/") or "0"
    query_databases = parse_qs(parsed.query, keep_blank_values=True).get("db", [])
    database = query_databases[0] if len(query_databases) == 1 else path_database
    return RedisLogTarget(
        host=parsed.hostname or "localhost",
        port=parsed.port,
        database=database if database.isdecimal() else "non-default",
        tls=parsed.scheme == "rediss",
    )


def _log_redis_connection(redis_url: str) -> None:
    target = _redis_log_target(redis_url)
    logger.info(
        "Redis connection initialized (host=%s port=%s db=%s tls=%s)",
        target.host,
        target.port if target.port is not None else "default",
        target.database,
        "enabled" if target.tls else "disabled",
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    postgres_url = os.environ.get(
        "POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl"
    )
    application.state.trusted_proxy_networks = parse_trusted_proxy_cidrs(
        os.environ.get("INGESTION_TRUSTED_PROXY_CIDRS", "")
    )
    r = aioredis.from_url(redis_url)
    try:
        auth_pool = await asyncpg.create_pool(
            postgres_url,
            min_size=1,
            max_size=5,
            init=_acquire_maintenance_inhibitor,
            reset=_reset_maintenance_inhibitor,
            max_inactive_connection_lifetime=0,
        )
    except BaseException:
        await r.aclose()
        raise
    maintenance_connection = None
    try:
        maintenance_connection = await auth_pool.acquire()
        maintenance_task, maintenance_listener = await _start_maintenance_monitor(
            maintenance_connection
        )
    except BaseException:
        try:
            if maintenance_connection is not None:
                await maintenance_connection.close()
        finally:
            try:
                await auth_pool.close()
            finally:
                await r.aclose()
        raise
    try:
        application.state.redis = r
        application.state.auth_pool = auth_pool
        application.state.authenticator = PostgresAuthenticator(auth_pool)
        _log_redis_connection(redis_url)
        yield
    finally:
        try:
            await r.aclose()
        finally:
            try:
                await _close_maintenance_monitor(
                    maintenance_connection,
                    maintenance_task,
                    maintenance_listener,
                )
            finally:
                await auth_pool.close()
    logger.info("Redis connection closed")


app = FastAPI(
    title="APDL Ingestion Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=MAX_REQUEST_BYTES,
)
app.add_middleware(PreAuthRateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router, dependencies=[Depends(authenticate_request)])


@app.get("/health")
async def health_check():
    """Liveness/readiness probe -- checks Redis connectivity."""
    try:
        await app.state.redis.ping()
        async with app.state.auth_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "service": "ingestion"}
    except Exception:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "service": "ingestion"},
        )
