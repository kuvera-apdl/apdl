"""APDL Query Service — FastAPI application entry point."""

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
from app.routers import cohorts, events, experiments, funnels, guardrails, retention

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    client = ClickHouseClient()
    try:
        await client.connect()
        postgres_url = os.environ.get(
            "POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl"
        )
        auth_pool = await asyncpg.create_pool(postgres_url, min_size=1, max_size=5)
    except Exception:
        await client.close()
        raise
    application.state.ch_client = client
    application.state.auth_pool = auth_pool
    application.state.authenticator = PostgresAuthenticator(auth_pool)
    logger.info("ClickHouse connection pool initialized")
    try:
        yield
    finally:
        try:
            await client.close()
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
    """Readiness probe — verifies ClickHouse connectivity."""
    try:
        client: ClickHouseClient = app.state.ch_client
        await client.execute("SELECT 1", {})
        async with app.state.auth_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready"}
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "not_ready"})
