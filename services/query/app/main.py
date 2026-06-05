"""APDL Query Service — FastAPI application entry point."""

import asyncio
import os
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.clickhouse.client import ClickHouseClient
from app.guardrails.monitor import run_guardrail_monitor
from app.routers import cohorts, events, experiments, funnels, guardrails, retention

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    client = ClickHouseClient()
    await client.connect()
    application.state.ch_client = client
    logger.info("ClickHouse connection pool initialized")
    monitor_task = _start_guardrail_monitor(client)
    application.state.guardrail_monitor_task = monitor_task
    yield
    if monitor_task is not None:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
    await client.close()
    logger.info("ClickHouse connection pool closed")


app = FastAPI(
    title="APDL Query Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router)
app.include_router(funnels.router)
app.include_router(cohorts.router)
app.include_router(retention.router)
app.include_router(experiments.router)
app.include_router(guardrails.router)


def _start_guardrail_monitor(client: ClickHouseClient) -> asyncio.Task | None:
    enabled = os.getenv("GUARDRAIL_MONITOR_ENABLED", "false").lower() == "true"
    project_ids = [
        project_id.strip()
        for project_id in os.getenv("GUARDRAIL_PROJECT_IDS", "").split(",")
        if project_id.strip()
    ]
    if not enabled or not project_ids:
        return None

    interval_seconds = int(os.getenv("GUARDRAIL_MONITOR_INTERVAL_SECONDS", "60"))
    config_service_url = os.getenv("CONFIG_SERVICE_URL", "http://localhost:8081")
    logger.info(
        "Starting guardrail monitor for %d projects every %ds",
        len(project_ids),
        interval_seconds,
    )
    return asyncio.create_task(
        run_guardrail_monitor(
            ch_client=client,
            config_service_url=config_service_url,
            project_ids=project_ids,
            interval_seconds=interval_seconds,
        )
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
        return {"status": "ready"}
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        return {"status": "not_ready", "error": str(exc)}
