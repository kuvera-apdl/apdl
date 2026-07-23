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
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth import PostgresAuthenticator, authenticate_request
from app.config import (
    codegen_ci_poll_interval,
    codegen_controller_image_id,
    codegen_cors_origins,
    codegen_development_mode,
    codegen_egress_policy_sha256,
    codegen_egress_proxy_image_id,
    codegen_egress_socket_volume,
    codegen_job_budget,
    codegen_max_concurrent_jobs,
    codegen_model,
    codegen_revision,
    codegen_rollout_authorization_path,
    codegen_rollout_stage,
    codegen_sandbox_mode,
    codegen_sandbox_image,
    codegen_sandbox_network,
    codegen_stale_sweep_interval,
    codegen_trusted_repos_only,
    postgres_url,
)
from app.db import assert_schema_ready
from app.editor.aider_editor import AiderEditor
from app.editor.base import Editor
from app.editor.container_editor import ContainerAiderEditor
from app.editor.environment import codegen_behavior_configuration_sha256
from app.evaluations.models import CodegenCandidateIdentity, RolloutStage
from app.evaluations.publication import load_publication_authorizer
from app.github.checks import get_ci_evidence
from app.github.publisher import GitBranchPublisher
from app.github.pulls import (
    close_pull_request,
    find_pull_request_by_branch,
    get_pull_request,
    open_pull_request,
)
from app.github.token_broker import GitHubTokenBroker
from app.jobs.ci_poller import run_github_poller
from app.jobs.pr_publication import resume_pull_request_publication
from app.jobs.repair import repair_failed_ci
from app.jobs.runner import run_changeset_job, run_stale_sweeper
from app.models.observations import CIVerificationObservation
from app.publication import ConfiguredPublicationGate
from app.routers import changesets, connections, webhooks
from app.runtime.collector import collect_runtime_evidence
from app.safety.policy import load_platform_safety_policy
from app.store import changesets as changeset_store
from app.store import pr_publication as publication_store

#: Error recorded on changesets the orphan sweeps fail (startup + periodic).
_ORPHAN_ERROR = (
    "Orphaned mid-pipeline (codegen restarted or the job died); no PR was produced."
)

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


ChangesetCreationCapability = Literal["available", "disabled"]


def _make_editor(stage: RolloutStage | None = None) -> Editor:
    """Pick the editor execution model from ``CODEGEN_SANDBOX``.

    The isolated Docker worker is the default. In-process execution is available
    only for explicitly trusted local repositories while publication is disabled.
    """
    resolved_stage = stage or codegen_rollout_stage()
    mode = codegen_sandbox_mode()
    publication_stage = resolved_stage in {
        RolloutStage.development_pr,
        RolloutStage.reviewed_pr,
        RolloutStage.low_risk_canary,
    }
    if mode == "docker":
        network = codegen_sandbox_network()
        invalid_network = network in {
            "",
            "bridge",
            "default",
            "host",
            "none",
        }
        if resolved_stage is RolloutStage.development_pr and invalid_network:
            raise RuntimeError(
                "development_pr requires CODEGEN_SANDBOX_NETWORK to name a "
                "dedicated local sandbox network"
            )
        if (
            resolved_stage in {RolloutStage.reviewed_pr, RolloutStage.low_risk_canary}
            and network
        ):
            raise RuntimeError(
                "evaluated PR rollout stages use Docker --network none; "
                "CODEGEN_SANDBOX_NETWORK must be empty"
            )
        logger.info(
            "Codegen editor: sandboxed container execution (CODEGEN_SANDBOX=docker)"
        )
        editor = ContainerAiderEditor()
        if resolved_stage is RolloutStage.development_pr:
            editor.assert_runtime_ready(
                expected_revision=codegen_revision(),
                require_immutable_image=False,
                require_egress_policy=False,
            )
        elif publication_stage:
            editor.assert_runtime_ready(expected_revision=codegen_revision())
        return editor
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
    """Build the explicit development gate or load evaluated rollout evidence."""
    stage = codegen_rollout_stage()
    model = codegen_model()
    revision = codegen_revision()
    development_mode = codegen_development_mode()
    provider = None
    candidate_identity = None
    egress_policy_sha256 = None
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
        egress_policy_sha256 = codegen_egress_policy_sha256()
        if not egress_policy_sha256:
            raise RuntimeError(
                "CODEGEN_EGRESS_POLICY_SHA256 is required for PR rollout stages"
            )
        egress_proxy_image_id = codegen_egress_proxy_image_id()
        if not egress_proxy_image_id:
            raise RuntimeError(
                "CODEGEN_EGRESS_PROXY_IMAGE_ID is required for PR rollout stages"
            )
        if not codegen_egress_socket_volume():
            raise RuntimeError(
                "CODEGEN_EGRESS_SOCKET_VOLUME is required for PR rollout stages"
            )
        reviewed_max_concurrent_jobs = codegen_max_concurrent_jobs()
        if reviewed_max_concurrent_jobs != 1:
            raise RuntimeError(
                "evaluated PR rollout stages require CODEGEN_MAX_CONCURRENT_JOBS=1"
            )
        candidate_identity = CodegenCandidateIdentity.build(
            controller_image_id=codegen_controller_image_id(),
            candidate_image_id=codegen_sandbox_image(),
            codegen_revision=revision,
            behavior_configuration_sha256=(codegen_behavior_configuration_sha256()),
            egress_policy_sha256=egress_policy_sha256,
            egress_proxy_image_id=egress_proxy_image_id,
            reviewed_max_concurrent_jobs=reviewed_max_concurrent_jobs,
        )
        provider = load_publication_authorizer(
            artifact_path,
            expected_model=model,
            expected_codegen_revision=revision,
            expected_candidate_identity_sha256=candidate_identity.identity_sha256,
            expected_egress_policy_sha256=egress_policy_sha256,
        )
    elif stage is RolloutStage.development_pr:
        if not development_mode:
            raise RuntimeError("development_pr requires CODEGEN_DEVELOPMENT_MODE=true")
        if codegen_rollout_authorization_path():
            raise RuntimeError(
                "development_pr must not receive an evaluated rollout bundle"
            )
    elif development_mode:
        raise RuntimeError(
            "CODEGEN_DEVELOPMENT_MODE=true is valid only with development_pr"
        )
    return ConfiguredPublicationGate(
        stage=stage,
        model=model,
        codegen_revision=revision,
        candidate_identity_sha256=(
            candidate_identity.identity_sha256
            if candidate_identity is not None
            else None
        ),
        egress_policy_sha256=egress_policy_sha256,
        provider=provider,
        development_mode=development_mode,
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    platform_safety_policy = load_platform_safety_policy()
    application.state.platform_codegen_safety_policy = platform_safety_policy
    publication_gate = _make_publication_gate()
    application.state.codegen_rollout_stage = publication_gate.stage
    # Attest the exact evaluated worker/proxy topology before opening database
    # or GitHub-token lifecycle resources.
    editor = _make_editor(publication_gate.stage)
    # A recovering publication owns one session-level advisory lock connection.
    # Keep independent capacity for token minting, the broker listener, and API
    # traffic even when every configured worker is inside publication recovery.
    pool_max_size = max(10, codegen_max_concurrent_jobs() + 4)
    pool = await asyncpg.create_pool(
        postgres_url(),
        min_size=2,
        max_size=pool_max_size,
        init=_acquire_maintenance_inhibitor,
        reset=_reset_maintenance_inhibitor,
        max_inactive_connection_lifetime=0,
    )
    token_broker = None
    maintenance_connection = None
    maintenance_task = None
    maintenance_listener = None
    repair_jobs: set[asyncio.Task] = set()
    requeued_jobs: list[asyncio.Task] = []
    poller_task: asyncio.Task | None = None
    sweeper_task: asyncio.Task | None = None
    try:
        application.state.pg_pool = pool
        application.state.authenticator = PostgresAuthenticator(pool)
        token_broker = GitHubTokenBroker(pool)
        application.state.github_token_broker = token_broker
        maintenance_connection = await pool.acquire()
        maintenance_task, maintenance_listener = await _start_maintenance_monitor(
            maintenance_connection
        )

        async with pool.acquire() as conn:
            await assert_schema_ready(conn)
        await token_broker.start()
        branch_publisher = GitBranchPublisher()
        publication_recovery_deps = {
            "mint_read_token": token_broker.read_changeset,
            "mint_write_token": token_broker.write_changeset,
            "mint_pr_write_token": token_broker.pr_write_changeset,
            "branch_publisher": branch_publisher,
            "open_pr": open_pull_request,
            "find_pr": find_pull_request_by_branch,
            "close_pr": close_pull_request,
        }

        # Recover orphans: in-process background jobs can't survive a restart, so any
        # changeset left in an active (post-claim, pre-PR) state from before this
        # boot is dead. Sweep it to error rather than let it hang in a non-terminal
        # status forever. The deadline (2× the full per-job pipeline budget) keeps a
        # concurrent replica's in-flight work safe on the shared database; rows
        # younger than that are caught by the periodic sweeper once they age out.
        orphan_deadline = 2 * codegen_job_budget()
        recoverable_publications = await publication_store.list_recoverable_ids(
            pool,
            # Every intent predates this fresh process. Recovery is idempotent and
            # branch-scoped, so resume immediately instead of waiting a job budget.
            older_than_seconds=0,
        )
        for changeset_id in recoverable_publications:
            await resume_pull_request_publication(
                pool,
                changeset_id,
                **publication_recovery_deps,
            )
        swept = await changeset_store.fail_stale_changesets(
            pool,
            older_than_seconds=orphan_deadline,
            error=_ORPHAN_ERROR,
        )
        if swept:
            logger.warning(
                "Swept %d orphaned changeset(s) to error: %s",
                len(swept),
                ", ".join(swept),
            )

        # Dependencies for the changeset job runner (editing engine + PR opener).

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
                    mint_read_token=token_broker.read_changeset,
                    mint_write_token=token_broker.write_changeset,
                    branch_publisher=branch_publisher,
                    publication_gate=publication_gate,
                    platform_safety_policy=platform_safety_policy,
                )
            )
            repair_jobs.add(task)
            task.add_done_callback(_repair_finished)

        application.state.repair_jobs = repair_jobs
        application.state.job_deps = {
            "editor": editor,
            "mint_read_token": token_broker.read_changeset,
            "mint_write_token": token_broker.write_changeset,
            "mint_pr_write_token": token_broker.pr_write_changeset,
            "branch_publisher": branch_publisher,
            "open_pr": open_pull_request,
            "find_pr": find_pull_request_by_branch,
            "close_pr": close_pull_request,
            "publication_gate": publication_gate,
            "platform_safety_policy": platform_safety_policy,
        }

        # Re-enqueue work a restart orphaned before it began: a queued row produced
        # nothing yet, so re-running it is safe, and the job's queued → cloning
        # claim transition guarantees a single winner even with a concurrent
        # replica doing the same. (References are held so the tasks aren't GC'd.)
        queued_ids = await changeset_store.list_queued_changeset_ids(pool)
        for changeset_id in queued_ids:
            requeued_jobs.append(
                asyncio.create_task(
                    run_changeset_job(
                        pool,
                        changeset_id,
                        **application.state.job_deps,
                    )
                )
            )
        application.state.requeued_jobs = requeued_jobs
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
            "mint_token": token_broker.read_changeset,
            "repair_failure": _schedule_ci_repair,
            "collect_runtime": collect_runtime_evidence,
        }

        # CI poller: the zero-config trigger that keeps open changesets advancing
        # without an inbound webhook (the common self-hosted case). Disabled when the
        # interval is 0 — e.g. once a low-latency GitHub webhook is wired instead.
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
        sweep_interval = codegen_stale_sweep_interval()
        if sweep_interval > 0:
            sweeper_task = asyncio.create_task(
                run_stale_sweeper(
                    pool,
                    interval_seconds=sweep_interval,
                    older_than_seconds=orphan_deadline,
                    error=_ORPHAN_ERROR,
                    **publication_recovery_deps,
                )
            )
        else:
            logger.info("Stale sweeper disabled (CODEGEN_STALE_SWEEP_INTERVAL=0)")

        logger.info("Codegen service started: PostgreSQL schema migration verified")
        yield
    finally:
        periodic_tasks = [
            task for task in (poller_task, sweeper_task) if task is not None
        ]
        for task in periodic_tasks:
            task.cancel()
        if periodic_tasks:
            await asyncio.gather(*periodic_tasks, return_exceptions=True)
        for task in tuple(repair_jobs):
            task.cancel()
        if repair_jobs:
            await asyncio.gather(*repair_jobs, return_exceptions=True)
        for task in requeued_jobs:
            task.cancel()
        if requeued_jobs:
            # A requeued editor may still own a GitHub token lease and an isolated
            # worker. Await cancellation while PostgreSQL and broker dependencies
            # are alive so its context manager can revoke the credential cleanly.
            await asyncio.gather(*requeued_jobs, return_exceptions=True)
        if token_broker is not None:
            await token_broker.close()
        if maintenance_task is not None:
            await _close_maintenance_monitor(
                maintenance_connection,
                maintenance_task,
                maintenance_listener,
            )
        elif maintenance_connection is not None:
            await maintenance_connection.close()
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
app.include_router(webhooks.router)


@app.get("/health")
async def health_check():
    """Liveness probe."""
    return {"status": "ok", "service": "apdl-codegen"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe with the canonical changeset-creation capability.

    Returns 503 (not 200-with-a-sad-body) on failure: orchestrators and load
    balancers key on the status code, not the payload.  Process readiness and
    publication authority are deliberately separate: offline/shadow Codegen is
    healthy, but callers must not offer or enqueue changeset mutations.
    """
    stage = getattr(app.state, "codegen_rollout_stage", None)
    if not isinstance(stage, RolloutStage):
        stage = codegen_rollout_stage()
    changeset_creation: ChangesetCreationCapability = (
        "disabled"
        if stage in {RolloutStage.offline, RolloutStage.shadow}
        else "available"
    )
    capabilities = {"changeset_creation": changeset_creation}
    try:
        pool: asyncpg.Pool = app.state.pg_pool
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {
            "status": "ready",
            "service": "apdl-codegen",
            "capabilities": capabilities,
        }
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "apdl-codegen",
                "capabilities": capabilities,
                "error": str(exc),
            },
        )
