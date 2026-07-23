"""APDL Query Service — FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import PostgresAuthenticator, authenticate_request
from app.clickhouse.client import (
    ClickHouseClient,
    QueryBudgetExceeded,
    QueryConcurrencyExceeded,
)
from app.config_client import assert_experiment_analysis_capability
from app.readiness import (
    assert_clickhouse_decision_schema,
    assert_decision_dependencies_ready,
    assert_postgres_decision_schema,
)
from app.routers import cohorts, events, experiments, funnels, guardrails, retention

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


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    client = ClickHouseClient()
    auth_pool = None
    maintenance_connection = None
    try:
        await client.connect()
        postgres_url = os.environ.get(
            "POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl"
        )
        auth_pool = await asyncpg.create_pool(
            postgres_url,
            min_size=1,
            max_size=5,
            init=_acquire_maintenance_inhibitor,
            reset=_reset_maintenance_inhibitor,
            max_inactive_connection_lifetime=0,
        )
        await assert_decision_dependencies_ready(client, auth_pool)
        logger.info("Experiment decision dependencies verified")
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
                if auth_pool is not None:
                    await auth_pool.close()
            finally:
                await client.close()
        raise
    try:
        application.state.ch_client = client
        application.state.auth_pool = auth_pool
        application.state.completeness_pool = auth_pool
        application.state.authenticator = PostgresAuthenticator(auth_pool)
        logger.info("ClickHouse connection pool initialized")
        yield
    finally:
        try:
            await client.close()
        finally:
            try:
                await _close_maintenance_monitor(
                    maintenance_connection,
                    maintenance_task,
                    maintenance_listener,
                )
            finally:
                await auth_pool.close()
    logger.info("ClickHouse connection pool closed")


app = FastAPI(
    title="APDL Query Service",
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
app.include_router(events.router, dependencies=auth_dependencies)
app.include_router(funnels.router, dependencies=auth_dependencies)
app.include_router(cohorts.router, dependencies=auth_dependencies)
app.include_router(retention.router, dependencies=auth_dependencies)
app.include_router(experiments.router, dependencies=auth_dependencies)
app.include_router(guardrails.router, dependencies=auth_dependencies)


@app.exception_handler(QueryConcurrencyExceeded)
async def query_concurrency_exceeded(_, exc: QueryConcurrencyExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "query_concurrency_exceeded", "message": str(exc)},
    )


@app.exception_handler(QueryBudgetExceeded)
async def query_budget_exceeded(_, exc: QueryBudgetExceeded):
    return JSONResponse(
        status_code=503,
        content={"error": "query_budget_exceeded", "message": str(exc)},
    )


@app.get("/health")
async def health_check():
    """Liveness probe — returns 200 if the service is running."""
    return {"status": "ok", "service": "apdl-query"}


@app.get("/ready")
async def readiness_check():
    """Fail closed unless every final-decision capability is still usable."""
    client: ClickHouseClient = app.state.ch_client
    checks_to_run = (
        ("clickhouse_schema", assert_clickhouse_decision_schema(client)),
        ("postgres_schema", assert_postgres_decision_schema(app.state.auth_pool)),
        ("config_analysis", assert_experiment_analysis_capability()),
    )
    results = await asyncio.gather(
        *(check for _, check in checks_to_run),
        return_exceptions=True,
    )
    checks = {}
    for (name, _), result in zip(checks_to_run, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.error(
                "Readiness capability %s failed: %s",
                name,
                type(result).__name__,
            )
            checks[name] = "not_ready"
        else:
            checks[name] = "ready"

    payload = {
        "status": "ready",
        "service": "apdl-query",
        "checks": checks,
    }
    if any(value != "ready" for value in checks.values()):
        payload["status"] = "not_ready"
        return JSONResponse(status_code=503, content=payload)
    return payload
