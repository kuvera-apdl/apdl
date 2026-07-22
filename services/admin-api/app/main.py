"""APDL admin backend-for-frontend application."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import auth, credentials, projects, proxy
from app.config import Settings

logger = logging.getLogger(__name__)

ReadinessState = Literal["ready", "not_ready"]
_CORE_UPSTREAMS = {
    "ingestion": ("/health", "ok"),
    "config": ("/ready", "ready"),
    "query": ("/ready", "ready"),
}
_CAPABILITY_UPSTREAMS = {
    "agents": ("/ready", "ready"),
    "codegen": ("/ready", "ready"),
}
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


def _upstream_timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        30.0,
        connect=5.0,
        read=settings.upstream_read_timeout_seconds,
    )


async def _probe_postgres(pool) -> ReadinessState:
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        return "not_ready"
    return "ready"


async def _probe_upstream(
    client: httpx.AsyncClient,
    settings: Settings,
    service: str,
    path: str,
    expected_status: str,
) -> ReadinessState:
    try:
        response = await client.get(
            f"{settings.service_urls[service]}{path}",
            timeout=settings.readiness_probe_timeout_seconds,
        )
        body = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return "not_ready"
    if (
        response.status_code == 200
        and isinstance(body, dict)
        and "status" in body
        and body["status"] == expected_status
    ):
        return "ready"
    return "not_ready"


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_env()
    pool = await asyncpg.create_pool(
        settings.postgres_url,
        min_size=2,
        max_size=10,
        init=_acquire_maintenance_inhibitor,
        reset=_reset_maintenance_inhibitor,
        max_inactive_connection_lifetime=0,
    )
    maintenance_connection = None
    maintenance_task = None
    maintenance_listener = None
    client = None
    try:
        maintenance_connection = await pool.acquire()
        maintenance_task, maintenance_listener = await _start_maintenance_monitor(
            maintenance_connection
        )
        client = httpx.AsyncClient(
            timeout=_upstream_timeout(settings), follow_redirects=False
        )
        application.state.settings = settings
        application.state.pg_pool = pool
        application.state.http_client = client
        yield
    finally:
        if client is not None:
            await client.aclose()
        if maintenance_task is not None:
            await _close_maintenance_monitor(
                maintenance_connection,
                maintenance_task,
                maintenance_listener,
            )
        elif maintenance_connection is not None:
            await maintenance_connection.close()
        await pool.close()


app = FastAPI(
    title="APDL Admin API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(credentials.router)
app.include_router(proxy.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "apdl-admin-api"}


@app.get("/api/ready")
async def ready(request: Request):
    core: dict[str, ReadinessState] = {
        "postgres": "not_ready",
        **dict.fromkeys(_CORE_UPSTREAMS, "not_ready"),
    }
    capabilities: dict[str, ReadinessState] = dict.fromkeys(
        _CAPABILITY_UPSTREAMS, "not_ready"
    )
    settings = request.app.state.settings
    upstreams = {**_CORE_UPSTREAMS, **_CAPABILITY_UPSTREAMS}
    postgres_result, *results = await asyncio.gather(
        _probe_postgres(request.app.state.pg_pool),
        *(
            _probe_upstream(
                request.app.state.http_client,
                settings,
                service,
                path,
                expected_status,
            )
            for service, (path, expected_status) in upstreams.items()
        ),
    )
    core["postgres"] = postgres_result
    for service, result in zip(upstreams, results, strict=True):
        if service in core:
            core[service] = result
        else:
            capabilities[service] = result

    degraded = any(value != "ready" for value in capabilities.values())
    payload = {
        "status": "ready",
        "degraded": degraded,
        "core": core,
        "capabilities": capabilities,
    }
    if any(value != "ready" for value in core.values()):
        payload["status"] = "not_ready"
        return JSONResponse(status_code=503, content=payload)
    return payload
