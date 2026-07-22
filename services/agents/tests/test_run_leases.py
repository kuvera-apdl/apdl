"""Multi-worker ownership and abandoned-run recovery regression tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.main as agents_main
from app.graphs import supervisor
from app.graphs.supervisor import _update_run
from app.store.proposals import claim_proposals
from app.store import run_leases
from app.store.run_leases import (
    RunLeaseLostError,
    acquire_run_lease,
    handoff_run_to_queue,
    maintain_run_lease,
    requeue_expired_runs,
    renew_run_lease,
)


def _is_active(run: dict[str, Any]) -> bool:
    return run["status"] in {"started", "running"} or (
        run["phase"] == "resuming" and run["status"] in {"approved", "rejected"}
    )


def _holds_execution_lane(run: dict[str, Any]) -> bool:
    return run["status"] not in {
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
        "manual_intervention",
    }


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(
        self,
        now: datetime,
        runs: dict[str, dict[str, Any]],
        proposals: dict[str, dict[str, Any]],
    ) -> None:
        self.now = now
        self.runs = runs
        self.proposals = proposals
        self.renewals = 0
        self.termination_listeners: list[Any] = []

    def add_termination_listener(self, listener: Any) -> None:
        self.termination_listeners.append(listener)

    def remove_termination_listener(self, listener: Any) -> None:
        self.termination_listeners.remove(listener)

    async def close(self) -> None:
        return None

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchval(self, query: str, *args: Any) -> str | bool | None:
        if "pg_catalog.pg_locks" in query:
            return True
        if "INSERT INTO agent_audit_log" in query:
            return "1"

        run_id = str(args[0])
        run = self.runs.get(run_id)
        if run is None:
            return None

        if "SET lease_owner_id = $2" in query:
            owner_id = str(args[1])
            lease_seconds = int(args[2])
            recovery_grace_seconds = int(args[3])
            expiry = run.get("lease_expires_at")
            if not _is_active(run) or not (
                run.get("lease_owner_id") is None
                or (
                    expiry is not None
                    and expiry <= self.now - timedelta(seconds=recovery_grace_seconds)
                )
            ):
                return None
            run["lease_owner_id"] = owner_id
            run["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            run["updated_at"] = self.now
            return run_id

        if "SET lease_expires_at" in query:
            self.renewals += 1
            owner_id = str(args[1])
            lease_seconds = int(args[2])
            if (
                not _is_active(run)
                or run.get("lease_owner_id") != owner_id
                or run.get("lease_expires_at") is None
                or run["lease_expires_at"] <= self.now
            ):
                return None
            run["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            run["updated_at"] = self.now
            return run_id

        if "phase = 'resuming'" in query and "SET lease_owner_id = NULL" in query:
            owner_id = str(args[1])
            lease_seconds = int(args[2])
            if (
                run.get("lease_owner_id") != owner_id
                or run.get("lease_expires_at") is None
                or run["lease_expires_at"] <= self.now
                or run.get("phase") != "resuming"
                or run.get("status") not in {"approved", "rejected"}
            ):
                return None
            run["lease_owner_id"] = None
            run["lease_expires_at"] = self.now + timedelta(seconds=lease_seconds)
            run["updated_at"] = self.now
            return run_id

        if "SET lease_owner_id = NULL" in query:
            if run.get("lease_owner_id") != str(args[1]):
                return None
            run["lease_owner_id"] = None
            run["lease_expires_at"] = None
            return run_id

        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "UPDATE agent_runs" in query and "SET lease_owner_id = NULL" in query:
            legacy_cutoff = self.now - timedelta(seconds=int(args[0]))
            recovery_cutoff = self.now - timedelta(seconds=int(args[1]))
            requeued: list[dict[str, Any]] = []
            for run_id, run in self.runs.items():
                expiry = run.get("lease_expires_at")
                expired = expiry is not None and expiry <= recovery_cutoff
                legacy_stale = expiry is None and run["updated_at"] <= legacy_cutoff
                if (
                    _is_active(run)
                    and run.get("lease_owner_id") is not None
                    and (expired or legacy_stale)
                ):
                    run.update(
                        lease_owner_id=None,
                        lease_expires_at=None,
                        updated_at=self.now,
                    )
                    requeued.append({"run_id": run_id})
            return requeued

        if "proposal.claim_run_id IS NULL" in query:
            stale_cutoff = self.now - timedelta(seconds=int(args[0]))
            active_projects = {
                str(run.get("project_id") or "demo")
                for run in self.runs.values()
                if _holds_execution_lane(run)
            }
            reopened: list[dict[str, Any]] = []
            for proposal_id, proposal in self.proposals.items():
                if (
                    proposal["status"] == "implementing"
                    and proposal.get("claim_run_id") is None
                    and proposal.get("updated_at", self.now) <= stale_cutoff
                    and proposal["project_id"] not in active_projects
                ):
                    proposal.update(
                        status="approved",
                        claim_run_id=None,
                        error=None,
                        updated_at=self.now,
                    )
                    reopened.append({"proposal_id": proposal_id})
            return reopened

        if "FROM agent_runs AS claim_run" in query:
            reopened: list[dict[str, Any]] = []
            for proposal_id, proposal in self.proposals.items():
                claim_run_id = proposal.get("claim_run_id")
                claim_run = self.runs.get(str(claim_run_id))
                if (
                    proposal["status"] == "implementing"
                    and claim_run is not None
                    and proposal["project_id"] == claim_run["project_id"]
                    and claim_run["status"]
                    in {"completed", "completed_with_errors", "failed"}
                ):
                    proposal.update(
                        status="approved",
                        claim_run_id=None,
                        error=None,
                        updated_at=self.now,
                    )
                    reopened.append({"proposal_id": proposal_id})
            return reopened

        if "FROM feature_proposals" in query:
            project_id = str(args[0])
            target_id = str(args[1]) if "proposal_id = $2" in query else None
            limit = int(args[1]) if target_id is None else None
            rows = [
                {
                    "proposal_id": proposal_id,
                    "title": proposal["title"],
                    "spec": proposal["spec"],
                    "priority": proposal.get("priority"),
                }
                for proposal_id, proposal in self.proposals.items()
                if proposal["project_id"] == project_id
                and proposal["status"] == "approved"
                and (target_id is None or proposal_id == target_id)
            ]
            return rows if limit is None else rows[:limit]

        raise AssertionError(f"Unexpected fetch query: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        if query.lstrip().startswith(("CREATE ", "ALTER ")):
            return "OK"

        if "SET status = 'implementing'" in query:
            project_id = str(args[0])
            proposal_ids = set(args[1])
            claim_run_id = str(args[2])
            for proposal_id, proposal in self.proposals.items():
                if proposal["project_id"] == project_id and proposal_id in proposal_ids:
                    proposal.update(
                        status="implementing",
                        claim_run_id=claim_run_id,
                        error=None,
                        updated_at=self.now,
                    )
            return f"UPDATE {len(proposal_ids)}"

        if "SET status = $2" in query:
            run = self.runs[str(args[0])]
            owner_id = str(args[5])
            if (
                run.get("lease_owner_id") != owner_id
                or run.get("lease_expires_at") is None
                or run["lease_expires_at"] <= self.now
            ):
                return "UPDATE 0"
            run.update(
                status=str(args[1]),
                phase=str(args[2]),
                updated_at=self.now,
            )
            run["execution_lane_project_id"] = (
                run["project_id"] if _holds_execution_lane(run) else None
            )
            if args[1] != "running":
                run["lease_owner_id"] = None
                run["lease_expires_at"] = None
            return "UPDATE 1"

        if "INSERT INTO agent_run_results" in query:
            return "INSERT 0 1"

        raise AssertionError(f"Unexpected execute query: {query}")


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    def __await__(self):
        async def acquire() -> _Conn:
            return self.conn

        return acquire().__await__()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(
        self,
        now: datetime,
        runs: dict[str, dict[str, Any]],
        proposals: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.conn = _Conn(now, runs, proposals or {})
        self.close_count = 0

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)

    async def close(self) -> None:
        self.close_count += 1


def _run(
    now: datetime,
    *,
    project_id: str = "demo",
    status: str = "running",
    phase: str = "behavior_analysis",
    owner: str | None = None,
    expires: datetime | None = None,
    updated: datetime | None = None,
) -> dict[str, Any]:
    run = {
        "project_id": project_id,
        "status": status,
        "phase": phase,
        "lease_owner_id": owner,
        "lease_expires_at": expires,
        "updated_at": updated or now,
    }
    run["execution_lane_project_id"] = (
        project_id if _holds_execution_lane(run) else None
    )
    return run


def _proposal(
    status: str,
    claim_run_id: str | None,
    *,
    project_id: str = "demo",
    updated_at: datetime | None = None,
) -> dict[str, Any]:
    proposal = {
        "project_id": project_id,
        "title": "Proposal",
        "spec": "Implement the proposal safely.",
        "priority": "P1",
        "status": status,
        "claim_run_id": claim_run_id,
        "error": None,
    }
    if updated_at is not None:
        proposal["updated_at"] = updated_at
    return proposal


def test_lease_migration_preserves_null_for_pre_upgrade_replicas() -> None:
    ddl = (
        Path(__file__).resolve().parents[3]
        / "pipeline"
        / "postgres"
        / "migrations"
        / "004_agents_core.sql"
    ).read_text()

    assert "ALTER COLUMN lease_expires_at" in ddl
    assert "DROP DEFAULT" in ddl
    assert "SET DEFAULT" not in ddl


@pytest.mark.asyncio
async def test_new_replica_cannot_claim_or_recover_another_live_worker() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "run-live": _run(
            now,
            owner="worker-a",
            expires=now + timedelta(seconds=60),
        )
    }
    proposals = {"proposal-live": _proposal("implementing", "run-live")}
    pool = _Pool(now, runs, proposals)

    assert not await acquire_run_lease(pool, "run-live", "worker-b")
    assert await renew_run_lease(pool, "run-live", "worker-a")

    recovered = await requeue_expired_runs(pool)

    assert recovered.requeued_run_ids == ()
    assert recovered.reopened_proposal_ids == ()
    assert runs["run-live"]["status"] == "running"
    assert runs["run-live"]["lease_owner_id"] == "worker-a"
    assert proposals["proposal-live"]["status"] == "implementing"


@pytest.mark.asyncio
async def test_starting_multiple_replicas_preserves_live_run_and_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "run-live": _run(
            now,
            owner="worker-a",
            expires=now + timedelta(seconds=60),
        )
    }
    proposals = {"proposal-live": _proposal("implementing", "run-live")}
    pool = _Pool(now, runs, proposals)

    async def fake_create_pool(*args: Any, **kwargs: Any) -> _Pool:
        return pool

    async def fake_schema_ready(conn: _Conn) -> None:
        return None

    async def idle_worker(*args: Any, **kwargs: Any) -> None:
        stop = next(arg for arg in args if isinstance(arg, asyncio.Event))
        await stop.wait()

    async def no_orphaned_llm_attempts(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(prepared_blocked=0, in_flight_cancelled=0)

    monkeypatch.setattr(agents_main.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(agents_main, "assert_schema_ready", fake_schema_ready)
    monkeypatch.setattr(agents_main, "requeue_expired_runs_forever", idle_worker)
    monkeypatch.setattr(agents_main, "dispatch_runs_forever", idle_worker)
    monkeypatch.setattr(
        agents_main, "run_approval_effect_worker_forever", idle_worker
    )
    monkeypatch.setattr(
        agents_main, "reconcile_orphaned_llm_attempts", no_orphaned_llm_attempts
    )
    monkeypatch.setattr(
        agents_main, "reconcile_orphaned_llm_attempts_forever", idle_worker
    )

    for _ in range(2):
        application = SimpleNamespace(state=SimpleNamespace())
        async with agents_main.lifespan(application):
            assert application.state.pg_pool is pool

    assert pool.close_count == 2
    assert runs["run-live"]["status"] == "running"
    assert runs["run-live"]["lease_owner_id"] == "worker-a"
    assert proposals["proposal-live"]["status"] == "implementing"


@pytest.mark.asyncio
async def test_expiry_recovers_only_the_abandoned_run_and_its_claim() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "run-dead": _run(
            now,
            owner="worker-dead",
            expires=now - timedelta(seconds=run_leases.RUN_RECOVERY_GRACE_SECONDS + 1),
        ),
        "run-live": _run(
            now,
            owner="worker-live",
            expires=now + timedelta(seconds=60),
        ),
        "run-gated": _run(
            now,
            status="waiting_approval",
            phase="code_implementation_approval",
            updated=now - timedelta(days=10),
        ),
        "run-effect": _run(
            now,
            project_id="effects",
            status="approval_queued",
            phase="code_implementation_approval",
            updated=now - timedelta(days=10),
        ),
    }
    proposals = {
        "proposal-dead": _proposal("implementing", "run-dead"),
        "proposal-live": _proposal("implementing", "run-live"),
        "proposal-gated": _proposal("implementing", "run-gated"),
        "proposal-effect": _proposal("implementing", None, project_id="effects"),
        "proposal-unowned": _proposal("implementing", None),
    }
    pool = _Pool(now, runs, proposals)

    recovered = await requeue_expired_runs(pool)

    assert recovered.requeued_run_ids == ("run-dead",)
    assert recovered.reopened_proposal_ids == ()
    assert runs["run-dead"]["status"] == "running"
    assert runs["run-dead"]["lease_owner_id"] is None
    assert runs["run-live"]["status"] == "running"
    assert runs["run-gated"]["status"] == "waiting_approval"
    assert proposals["proposal-dead"]["status"] == "implementing"
    assert proposals["proposal-live"]["status"] == "implementing"
    assert proposals["proposal-gated"]["status"] == "implementing"
    assert proposals["proposal-effect"]["status"] == "implementing"
    assert proposals["proposal-unowned"]["status"] == "implementing"

    assert (await requeue_expired_runs(pool)).requeued_run_ids == ()


@pytest.mark.asyncio
async def test_terminal_runs_reopen_only_their_implementing_proposal_claims() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "run-completed": _run(now, status="completed", phase="done"),
        "run-errors": _run(now, status="completed_with_errors", phase="done"),
        "run-failed": _run(now, status="failed", phase="invalid_config"),
        "run-live": _run(
            now,
            owner="worker-live",
            expires=now + timedelta(seconds=60),
        ),
        "run-gated": _run(
            now,
            status="waiting_approval",
            phase="code_implementation_approval",
        ),
    }
    proposals = {
        "proposal-completed": _proposal("implementing", "run-completed"),
        "proposal-errors": _proposal("implementing", "run-errors"),
        "proposal-failed": _proposal("implementing", "run-failed"),
        "proposal-live": _proposal("implementing", "run-live"),
        "proposal-gated": _proposal("implementing", "run-gated"),
        "proposal-terminal": _proposal("implemented", "run-completed"),
        "proposal-other-project": _proposal(
            "implementing",
            "run-completed",
            project_id="other",
        ),
    }
    pool = _Pool(now, runs, proposals)

    recovered = await requeue_expired_runs(pool)

    assert recovered.requeued_run_ids == ()
    assert recovered.reopened_proposal_ids == (
        "proposal-completed",
        "proposal-errors",
        "proposal-failed",
    )
    for proposal_id in recovered.reopened_proposal_ids:
        assert proposals[proposal_id]["status"] == "approved"
        assert proposals[proposal_id]["claim_run_id"] is None
    assert proposals["proposal-live"]["status"] == "implementing"
    assert proposals["proposal-gated"]["status"] == "implementing"
    assert proposals["proposal-terminal"]["status"] == "implemented"
    assert proposals["proposal-other-project"]["status"] == "implementing"


@pytest.mark.asyncio
async def test_legacy_unleased_runs_use_a_conservative_expiry() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "legacy-live": _run(now, updated=now - timedelta(hours=1)),
        "legacy-dead": _run(now, updated=now - timedelta(hours=25)),
        "legacy-owned-dead": _run(
            now,
            owner="legacy-worker",
            updated=now - timedelta(hours=25),
        ),
    }
    pool = _Pool(now, runs)

    assert not await acquire_run_lease(pool, "legacy-owned-dead", "worker-new")
    recovered = await requeue_expired_runs(pool)

    assert recovered.requeued_run_ids == ("legacy-owned-dead",)
    assert runs["legacy-live"]["status"] == "running"


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_the_current_owner() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "run-live": _run(
            now,
            owner="worker-current",
            expires=now + timedelta(seconds=60),
        )
    }
    pool = _Pool(now, runs)

    with pytest.raises(RunLeaseLostError):
        await _update_run(
            pool,
            "run-live",
            "completed",
            "done",
            "worker-stale",
        )

    assert runs["run-live"]["status"] == "running"


@pytest.mark.asyncio
async def test_proposal_claim_records_the_implementing_run() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    proposals = {"proposal-1": _proposal("approved", None)}
    pool = _Pool(now, {}, proposals)

    claimed = await claim_proposals(pool, "demo", "run-implementing", limit=1)

    assert [row["proposal_id"] for row in claimed] == ["proposal-1"]
    assert proposals["proposal-1"]["status"] == "implementing"
    assert proposals["proposal-1"]["claim_run_id"] == "run-implementing"


@pytest.mark.asyncio
async def test_expired_owner_observes_grace_but_queued_handoff_is_immediate() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "run-owned": _run(
            now,
            owner="worker-old",
            expires=now - timedelta(seconds=1),
        ),
        "run-queued": _run(
            now,
            status="approved",
            phase="resuming",
            owner="approval-worker",
            expires=now + timedelta(seconds=60),
        ),
    }
    pool = _Pool(now, runs)

    assert not await acquire_run_lease(pool, "run-owned", "worker-new")
    assert (await requeue_expired_runs(pool)).requeued_run_ids == ()

    assert await handoff_run_to_queue(pool, "run-queued", "approval-worker")
    assert runs["run-queued"]["lease_owner_id"] is None
    assert await acquire_run_lease(pool, "run-queued", "resume-worker")
    assert runs["run-queued"]["lease_owner_id"] == "resume-worker"


@pytest.mark.asyncio
async def test_heartbeat_errors_still_lose_at_local_confirmed_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def failing_renew(*args: Any, **kwargs: Any) -> bool:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(run_leases, "renew_run_lease", failing_renew)
    stop = asyncio.Event()
    lost = asyncio.Event()

    await asyncio.wait_for(
        maintain_run_lease(
            object(),
            "run-errors",
            "worker-a",
            stop,
            lost,
            lease_seconds=0.5,
            heartbeat_seconds=0.01,
            expiry_safety_seconds=0,
        ),
        timeout=1.5,
    )

    assert attempts >= 2
    assert lost.is_set()


@pytest.mark.asyncio
async def test_heartbeat_hang_is_bounded_by_local_confirmed_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renew_started = asyncio.Event()

    async def hanging_renew(*args: Any, **kwargs: Any) -> bool:
        renew_started.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(run_leases, "renew_run_lease", hanging_renew)
    stop = asyncio.Event()
    lost = asyncio.Event()

    await asyncio.wait_for(
        maintain_run_lease(
            object(),
            "run-hang",
            "worker-a",
            stop,
            lost,
            lease_seconds=0.04,
            heartbeat_seconds=0,
            expiry_safety_seconds=0,
        ),
        timeout=0.5,
    )

    assert renew_started.is_set()
    assert lost.is_set()


@pytest.mark.asyncio
async def test_lost_supervisor_cancels_agent_and_retains_expired_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {"run-loss": _run(now, status="started", phase="initializing")}
    pool = _Pool(now, runs)
    effects: list[str] = []

    class _SlowAgent:
        name = "code_implementation"
        order = 50
        requires: tuple[str, ...] = ()
        produces = "changesets"

        def __init__(self) -> None:
            self.started = asyncio.Event()

        def requirements_met(self, state: dict[str, Any]) -> bool:
            return True

        async def run(self, ctx: Any, state: dict[str, Any]) -> Any:
            effects.append("codegen-started")
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                effects.append("codegen-cancelled")
                raise
            effects.append("config-after-loss")
            return SimpleNamespace(output=[], metadata={})

    agent = _SlowAgent()

    async def lose_during_agent(
        pool: Any,
        run_id: str,
        owner_id: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
        **kwargs: Any,
    ) -> None:
        await agent.started.wait()
        lost.set()
        await stop.wait()

    monkeypatch.setattr(supervisor, "new_lease_owner_id", lambda: "worker-loss")
    monkeypatch.setattr(supervisor, "maintain_run_lease", lose_during_agent)
    monkeypatch.setattr(supervisor, "is_registered", lambda name: True)
    monkeypatch.setattr(supervisor, "get_agent", lambda name: agent)

    await supervisor.run_supervisor(
        pool=pool,
        vector_store=object(),
        run_id="run-loss",
        project_id="demo",
        analysis_types=["code_implementation"],
        time_range_days=7,
        autonomy_level=3,
    )

    assert effects == ["codegen-started", "codegen-cancelled"]
    assert runs["run-loss"]["status"] == "running"
    assert runs["run-loss"]["lease_owner_id"] == "worker-loss"
    assert runs["run-loss"]["lease_expires_at"] is not None


@pytest.mark.asyncio
async def test_stale_legacy_null_claim_recovers_only_without_active_project() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    runs = {
        "active-demo": _run(
            now,
            project_id="demo",
            owner="worker-live",
            expires=now + timedelta(seconds=60),
        )
    }
    proposals = {
        "stale-idle": _proposal(
            "implementing",
            None,
            project_id="idle",
            updated_at=now - timedelta(hours=2),
        ),
        "stale-active": _proposal(
            "implementing",
            None,
            project_id="demo",
            updated_at=now - timedelta(hours=2),
        ),
        "fresh-idle": _proposal(
            "implementing",
            None,
            project_id="idle",
            updated_at=now - timedelta(minutes=5),
        ),
    }
    pool = _Pool(now, runs, proposals)

    recovered = await requeue_expired_runs(
        pool,
        legacy_proposal_stale_seconds=60 * 60,
    )

    assert recovered.reopened_proposal_ids == ("stale-idle",)
    assert proposals["stale-idle"]["status"] == "approved"
    assert proposals["stale-active"]["status"] == "implementing"
    assert proposals["fresh-idle"]["status"] == "implementing"
