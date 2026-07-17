"""APDL admin backend-for-frontend application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Literal

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import auth, credentials, projects, proxy
from app.config import Settings

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
    pool = await asyncpg.create_pool(settings.postgres_url, min_size=2, max_size=10)
    client = httpx.AsyncClient(
        timeout=_upstream_timeout(settings), follow_redirects=False
    )
    application.state.settings = settings
    application.state.pg_pool = pool
    application.state.http_client = client
    try:
        yield
    finally:
        await client.aclose()
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
