"""APDL Codegen Service — FastAPI application entry point.

The codegen service is the platform's "hands": it connects to customer
repositories, produces changesets (branch + commits + pull request), and —
under policy — merges them. It is the only component that holds the GitHub App
credentials and runs untrusted code in a sandbox, isolated from the rest of the
platform. Orchestration, autonomy gating, and approvals stay in the agents
service, which calls this one over the internal API.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import codegen_agent_timeout, postgres_url
from app.db import ALL_DDL
from app.editor.aider_editor import AiderEditor
from app.editor.base import Editor
from app.editor.container_editor import ContainerAiderEditor
from app.github.app_auth import mint_installation_token
from app.github.checks import get_ci_status
from app.github.pulls import mark_ready_for_review, open_pull_request
from app.routers import changesets, connections, webhooks
from app.store import changesets as changeset_store

logger = logging.getLogger(__name__)


async def _mint_token(installation_id: int) -> str:
    """Mint a short-lived installation token (string) for the changeset job."""
    token = await mint_installation_token(installation_id)
    return token.token


def _make_editor() -> Editor:
    """Pick the editor execution model from ``CODEGEN_SANDBOX``.

    ``docker`` runs each changeset in an isolated, ephemeral sandbox container
    (Option B); anything else uses the in-process subprocess editor (default).
    """
    if os.getenv("CODEGEN_SANDBOX", "").strip().lower() == "docker":
        logger.info("Codegen editor: sandboxed container execution (CODEGEN_SANDBOX=docker)")
        return ContainerAiderEditor()
    logger.info("Codegen editor: in-process subprocess execution")
    return AiderEditor()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    pool = await asyncpg.create_pool(postgres_url(), min_size=2, max_size=10)
    application.state.pg_pool = pool

    async with pool.acquire() as conn:
        for ddl in ALL_DDL:
            await conn.execute(ddl)

    # Recover orphans: in-process background jobs can't survive a restart, so any
    # changeset left in a transient (pre-PR) state from before this boot is dead.
    # Sweep it to error rather than let it hang in a non-terminal status forever.
    # The deadline (2× the per-job budget) keeps a concurrent replica's in-flight
    # work safe on the shared database.
    swept = await changeset_store.fail_stale_changesets(
        pool,
        older_than_seconds=2 * codegen_agent_timeout(),
        error="Orphaned by a codegen restart while mid-pipeline; no PR was produced.",
    )
    if swept:
        logger.warning(
            "Swept %d orphaned changeset(s) to error: %s", len(swept), ", ".join(swept)
        )

    # Dependencies for the changeset job runner (editing engine + PR opener).
    application.state.job_deps = {
        "editor": _make_editor(),
        "mint_token": _mint_token,
        "open_pr": open_pull_request,
    }
    # Dependencies for CI-status sync (driven by the GitHub webhook).
    application.state.ci_deps = {
        "get_status": get_ci_status,
        "mint_token": _mint_token,
        "mark_ready": mark_ready_for_review,
    }

    logger.info("Codegen service started: PostgreSQL pool and schema initialized")
    yield

    await pool.close()
    logger.info("Codegen service shut down: PostgreSQL pool closed")


app = FastAPI(
    title="APDL Codegen Service",
    version="0.1.0",
    lifespan=lifespan,
)

# The admin console (localhost:5174) calls these endpoints directly from the
# browser, so cross-origin requests must be permitted. Starlette reflects the
# request origin when allow_origins=["*"] and allow_credentials=True, matching
# the other services (config/query/agents).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(connections.router)
app.include_router(changesets.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health_check():
    """Liveness probe."""
    return {"status": "ok", "service": "apdl-codegen"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe — verifies PostgreSQL connectivity."""
    try:
        pool: asyncpg.Pool = app.state.pg_pool
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready"}
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        return {"status": "not_ready", "error": str(exc)}
