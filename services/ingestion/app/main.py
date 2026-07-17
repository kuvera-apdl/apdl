"""APDL Ingestion Service -- FastAPI application entry point."""

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
from app.routers import events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


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
        auth_pool = await asyncpg.create_pool(postgres_url, min_size=1, max_size=5)
    except Exception:
        await r.aclose()
        raise
    application.state.redis = r
    application.state.auth_pool = auth_pool
    application.state.authenticator = PostgresAuthenticator(auth_pool)
    _log_redis_connection(redis_url)
    try:
        yield
    finally:
        try:
            await r.aclose()
        finally:
            await auth_pool.close()
    logger.info("Redis connection closed")


app = FastAPI(
    title="APDL Ingestion Service",
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
