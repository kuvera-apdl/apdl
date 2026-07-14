"""APDL Ingestion Service -- FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import PostgresAuthenticator, authenticate_request
from app.routers import events

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    postgres_url = os.environ.get(
        "POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl"
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
    logger.info("Redis connection initialized (%s)", redis_url)
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
