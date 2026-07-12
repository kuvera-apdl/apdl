"""APDL Codegen Service — FastAPI application entry point.

The codegen service is the platform's "hands": it connects to customer
repositories and produces changesets (branch + commits + pull request). GitHub
owns CI verification and merge. This service holds the GitHub App
credentials and runs untrusted code in a sandbox, isolated from the rest of the
platform. Orchestration, autonomy gating, and approvals stay in the agents
service, which calls this one over the internal API.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import PostgresAuthenticator, authenticate_request
from app.config import (
    codegen_ci_poll_interval,
    codegen_cors_origins,
    codegen_job_budget,
    codegen_model,
    codegen_revision,
    codegen_rollout_authorization_path,
    codegen_rollout_stage,
    codegen_sandbox_mode,
    codegen_sandbox_network,
    codegen_stale_sweep_interval,
    codegen_trusted_repos_only,
    postgres_url,
)
from app.db import assert_schema_ready
from app.editor.aider_editor import AiderEditor
from app.editor.base import Editor
from app.editor.container_editor import ContainerAiderEditor
from app.evaluations.models import RolloutStage
from app.evaluations.publication import load_publication_authorizer
from app.github.app_auth import mint_token_for_repo
from app.github.checks import get_ci_evidence
from app.github.pulls import get_pull_request, open_pull_request
from app.jobs.ci_poller import run_github_poller
from app.jobs.repair import repair_failed_ci
from app.jobs.runner import run_changeset_job, run_stale_sweeper
from app.models.observations import CIVerificationObservation
from app.publication import ConfiguredPublicationGate
from app.routers import changesets, connections, github, webhooks
from app.runtime.collector import collect_runtime_evidence
from app.store import changesets as changeset_store

#: Error recorded on changesets the orphan sweeps fail (startup + periodic).
_ORPHAN_ERROR = (
    "Orphaned mid-pipeline (codegen restarted or the job died); no PR was produced."
)

logger = logging.getLogger(__name__)


async def _mint_token(installation_id: int, repo: str) -> str:
    """Mint a short-lived installation token (string) for the changeset job.

    Delegates to :func:`mint_token_for_repo`, which self-heals a stale (rotated)
    installation id on a 404 by re-resolving it from the repo.
    """
    token = await mint_token_for_repo(installation_id, repo)
    return token.token


def _make_editor(stage: RolloutStage | None = None) -> Editor:
    """Pick the editor execution model from ``CODEGEN_SANDBOX``.

    The isolated Docker worker is the default. In-process execution is available
    only for explicitly trusted local repositories while publication is disabled.
    """
    resolved_stage = stage or codegen_rollout_stage()
    mode = codegen_sandbox_mode()
    publication_stage = resolved_stage in {
        RolloutStage.reviewed_pr,
        RolloutStage.low_risk_canary,
    }
    if mode == "docker":
        network = codegen_sandbox_network()
        if publication_stage and network in {"", "bridge", "default", "host"}:
            raise RuntimeError(
                "PR rollout stages require CODEGEN_SANDBOX_NETWORK to name an "
                "operator-managed egress-filtered network"
            )
        logger.info("Codegen editor: sandboxed container execution (CODEGEN_SANDBOX=docker)")
        return ContainerAiderEditor()
    if publication_stage:
        raise RuntimeError("PR rollout stages require CODEGEN_SANDBOX=docker")
    if not codegen_trusted_repos_only():
        raise RuntimeError(
            "In-process codegen requires CODEGEN_TRUSTED_REPOS_ONLY=true and is "
            "limited to offline/shadow development"
        )
    logger.warning("Codegen editor: trusted-repository in-process development mode")
    return AiderEditor()


def _make_publication_gate() -> ConfiguredPublicationGate:
    """Load the operator artifact once and bind it to this deployed candidate."""
    stage = codegen_rollout_stage()
    model = codegen_model()
    revision = codegen_revision()
    provider = None
    if stage in {RolloutStage.reviewed_pr, RolloutStage.low_risk_canary}:
        raw_path = codegen_rollout_authorization_path()
        if not raw_path:
            raise RuntimeError(
                "CODEGEN_ROLLOUT_AUTHORIZATION_PATH is required for PR rollout stages"
            )
        artifact_path = Path(raw_path)
        if not artifact_path.is_absolute():
            raise RuntimeError(
                "CODEGEN_ROLLOUT_AUTHORIZATION_PATH must be an absolute path"
            )
        provider = load_publication_authorizer(
            artifact_path,
            expected_model=model,
            expected_codegen_revision=revision,
        )
    return ConfiguredPublicationGate(
        stage=stage,
        model=model,
        codegen_revision=revision,
        provider=provider,
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    publication_gate = _make_publication_gate()
    application.state.codegen_rollout_stage = publication_gate.stage
    pool = await asyncpg.create_pool(postgres_url(), min_size=2, max_size=10)
    application.state.pg_pool = pool
    application.state.authenticator = PostgresAuthenticator(pool)

    async with pool.acquire() as conn:
        await assert_schema_ready(conn)

    # Recover orphans: in-process background jobs can't survive a restart, so any
    # changeset left in an active (post-claim, pre-PR) state from before this
    # boot is dead. Sweep it to error rather than let it hang in a non-terminal
    # status forever. The deadline (2× the full per-job pipeline budget) keeps a
    # concurrent replica's in-flight work safe on the shared database; rows
    # younger than that are caught by the periodic sweeper once they age out.
    swept = await changeset_store.fail_stale_changesets(
        pool,
        older_than_seconds=2 * codegen_job_budget(),
        error=_ORPHAN_ERROR,
    )
    if swept:
        logger.warning(
            "Swept %d orphaned changeset(s) to error: %s", len(swept), ", ".join(swept)
        )

    # Dependencies for the changeset job runner (editing engine + PR opener).
    editor = _make_editor(publication_gate.stage)
    repair_jobs: set[asyncio.Task] = set()

    def _repair_finished(task: asyncio.Task) -> None:
        repair_jobs.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "CI remediation background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _schedule_ci_repair(observation: CIVerificationObservation) -> None:
        """Start a deduplicated repair without blocking CI observation sweeps."""
        task = asyncio.create_task(
            repair_failed_ci(
                pool,
                observation,
                editor=editor,
                mint_token=_mint_token,
                publication_gate=publication_gate,
            )
        )
        repair_jobs.add(task)
        task.add_done_callback(_repair_finished)

    application.state.repair_jobs = repair_jobs
    application.state.job_deps = {
        "editor": editor,
        "mint_token": _mint_token,
        "open_pr": open_pull_request,
        "publication_gate": publication_gate,
    }

    # Re-enqueue work a restart orphaned before it began: a queued row produced
    # nothing yet, so re-running it is safe, and the job's queued → cloning
    # claim transition guarantees a single winner even with a concurrent
    # replica doing the same. (References are held so the tasks aren't GC'd.)
    queued_ids = await changeset_store.list_queued_changeset_ids(pool)
    application.state.requeued_jobs = [
        asyncio.create_task(
            run_changeset_job(pool, changeset_id, **application.state.job_deps)
        )
        for changeset_id in queued_ids
    ]
    if queued_ids:
        logger.info(
            "Re-enqueued %d queued changeset(s) from before this boot: %s",
            len(queued_ids),
            ", ".join(queued_ids),
        )
    # Live GitHub recovery dependencies shared by polling and webhooks.
    application.state.github_sync_deps = {
        "get_pull_request": get_pull_request,
        "get_ci_evidence": get_ci_evidence,
        "mint_token": _mint_token,
        "repair_failure": _schedule_ci_repair,
        "collect_runtime": collect_runtime_evidence,
    }

    # CI poller: the zero-config trigger that keeps open changesets advancing
    # without an inbound webhook (the common self-hosted case). Disabled when the
    # interval is 0 — e.g. once a low-latency GitHub webhook is wired instead.
    poller_task: asyncio.Task | None = None
    poll_interval = codegen_ci_poll_interval()
    if poll_interval > 0:
        poller_task = asyncio.create_task(
            run_github_poller(
                pool,
                interval_seconds=poll_interval,
                **application.state.github_sync_deps,
            )
        )
    else:
        logger.info("CI poller disabled (CODEGEN_CI_POLL_INTERVAL=0)")

    # Periodic orphan sweep: catches active-state rows that were too young for
    # the startup sweep (e.g. orphaned minutes before a restart) once they age
    # past the deadline, without waiting for some future restart.
    sweeper_task: asyncio.Task | None = None
    sweep_interval = codegen_stale_sweep_interval()
    if sweep_interval > 0:
        sweeper_task = asyncio.create_task(
            run_stale_sweeper(
                pool,
                interval_seconds=sweep_interval,
                older_than_seconds=2 * codegen_job_budget(),
                error=_ORPHAN_ERROR,
            )
        )
    else:
        logger.info("Stale sweeper disabled (CODEGEN_STALE_SWEEP_INTERVAL=0)")

    logger.info("Codegen service started: PostgreSQL schema migration verified")
    yield

    for task in (poller_task, sweeper_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    for task in tuple(repair_jobs):
        task.cancel()
    if repair_jobs:
        await asyncio.gather(*repair_jobs, return_exceptions=True)
    await pool.close()
    logger.info("Codegen service shut down: PostgreSQL pool closed")


app = FastAPI(
    title="APDL Codegen Service",
    version="0.1.0",
    lifespan=lifespan,
)

# The admin console calls these endpoints directly from the browser, so CORS
# must be permitted — but this service opens PRs on customer repos, so it
# uses an explicit origin allow-list rather than wildcard-with-credentials (which
# would let any site issue credentialed cross-origin requests). Configure prod
# origins via CODEGEN_CORS_ORIGINS; defaults to the local admin-console origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=codegen_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

auth_dependencies = [Depends(authenticate_request)]
app.include_router(connections.router, dependencies=auth_dependencies)
app.include_router(changesets.router, dependencies=auth_dependencies)
app.include_router(github.router, dependencies=auth_dependencies)
app.include_router(webhooks.router)


@app.get("/health")
async def health_check():
    """Liveness probe."""
    return {"status": "ok", "service": "apdl-codegen"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe — verifies PostgreSQL connectivity.

    Returns 503 (not 200-with-a-sad-body) on failure: orchestrators and load
    balancers key on the status code, not the payload.
    """
    try:
        pool: asyncpg.Pool = app.state.pg_pool
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready"}
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "error": str(exc)}
        )
