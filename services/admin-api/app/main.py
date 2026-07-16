"""APDL admin backend-for-frontend application."""

from __future__ import annotations

from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import auth, projects, proxy
from app.config import Settings


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_env()
    pool = await asyncpg.create_pool(settings.postgres_url, min_size=2, max_size=10)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=False
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
app.include_router(proxy.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "apdl-admin-api"}


@app.get("/api/ready")
async def ready(request: Request):
    checks = {
        "postgres": "not_ready",
        "ingestion": "not_ready",
        "config": "not_ready",
        "query": "not_ready",
    }
    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ready"
    except Exception:
        pass

    settings = request.app.state.settings
    upstreams = (
        ("ingestion", "/health", "ok"),
        ("config", "/health", "ok"),
        ("query", "/ready", "ready"),
    )
    for service, path, expected_status in upstreams:
        try:
            response = await request.app.state.http_client.get(
                f"{settings.service_urls[service]}{path}"
            )
            body = response.json()
            if (
                response.status_code == 200
                and isinstance(body, dict)
                and body.get("status") == expected_status
            ):
                checks[service] = "ready"
        except (httpx.HTTPError, ValueError, TypeError):
            pass

    payload = {"status": "ready", "checks": checks}
    if any(value != "ready" for value in checks.values()):
        payload["status"] = "not_ready"
        return JSONResponse(status_code=503, content=payload)
    return payload
