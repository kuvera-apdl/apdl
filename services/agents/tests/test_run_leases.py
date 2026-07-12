"""Multi-worker ownership and abandoned-run recovery regression tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
    recover_abandoned_runs,
    renew_run_lease,
)


def _is_active(run: dict[str, Any]) -> bool:
    return run["status"] in {"started", "running"} or (
        run["phase"] == "resuming" and run["status"] in {"approved", "rejected"}
    )


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

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchval(self, query: str, *args: Any) -> str | None:
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
        if "SET status = 'failed'" in query:
            legacy_cutoff = self.now - timedelta(seconds=int(args[0]))
            recovery_cutoff = self.now - timedelta(seconds=int(args[1]))
            abandoned: list[dict[str, Any]] = []
            for run_id, run in self.runs.items():
                expiry = run.get("lease_expires_at")
                expired = expiry is not None and expiry <= recovery_cutoff
                legacy_stale = expiry is None and run["updated_at"] <= legacy_cutoff
                if _is_active(run) and (expired or legacy_stale):
                    run.update(
                        status="failed",
                        phase="orphaned",
                        lease_owner_id=None,
                        lease_expires_at=None,
                        updated_at=self.now,
                    )
                    abandoned.append({"run_id": run_id})
            return abandoned

        if "proposal.claim_run_id IS NULL" in query:
            stale_cutoff = self.now - timedelta(seconds=int(args[0]))
            active_projects = {
                str(run.get("project_id") or "demo")
                for run in self.runs.values()
                if _is_active(run) or run.get("status") == "waiting_approval"
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

        if "claim_run_id = ANY" in query:
            abandoned = set(args[0])
            reopened: list[dict[str, Any]] = []
            for proposal_id, proposal in self.proposals.items():
                if (
                    proposal["status"] == "implementing"
                    and proposal.get("claim_run_id") in abandoned
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
            proposal_ids = set(args[0])
            claim_run_id = str(args[1])
            for proposal_id, proposal in self.proposals.items():
                if proposal_id in proposal_ids:
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
    return {
        "project_id": project_id,
        "status": status,
        "phase": phase,
        "lease_owner_id": owner,
        "lease_expires_at": expires,
        "updated_at": updated or now,
    }


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
    ddl = run_leases.AGENT_RUN_LEASE_MIGRATE_DDL

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

    recovered = await recover_abandoned_runs(pool)

    assert recovered.abandoned_run_ids == ()
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

    async def fake_memory_schema(conn: _Conn) -> None:
        return None

    monkeypatch.setattr(agents_main.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(agents_main, "ensure_agent_memory_schema", fake_memory_schema)

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
    }
    proposals = {
        "proposal-dead": _proposal("implementing", "run-dead"),
        "proposal-live": _proposal("implementing", "run-live"),
        "proposal-gated": _proposal("implementing", "run-gated"),
        "proposal-unowned": _proposal("implementing", None),
    }
    pool = _Pool(now, runs, proposals)

    recovered = await recover_abandoned_runs(pool)

    assert recovered.abandoned_run_ids == ("run-dead",)
    assert recovered.reopened_proposal_ids == ("proposal-dead",)
    assert runs["run-dead"]["status"] == "failed"
    assert runs["run-live"]["status"] == "running"
    assert runs["run-gated"]["status"] == "waiting_approval"
    assert proposals["proposal-dead"]["status"] == "approved"
    assert proposals["proposal-live"]["status"] == "implementing"
    assert proposals["proposal-gated"]["status"] == "implementing"
    assert proposals["proposal-unowned"]["status"] == "implementing"

    assert (await recover_abandoned_runs(pool)).abandoned_run_ids == ()


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
    recovered = await recover_abandoned_runs(pool)

    assert recovered.abandoned_run_ids == ("legacy-dead", "legacy-owned-dead")
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
    assert (await recover_abandoned_runs(pool)).abandoned_run_ids == ()

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
            lease_seconds=0.04,
            heartbeat_seconds=0.005,
            expiry_safety_seconds=0,
        ),
        timeout=0.5,
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

    recovered = await recover_abandoned_runs(
        pool,
        legacy_proposal_stale_seconds=60 * 60,
    )

    assert recovered.reopened_proposal_ids == ("stale-idle",)
    assert proposals["stale-idle"]["status"] == "approved"
    assert proposals["stale-active"]["status"] == "implementing"
    assert proposals["fresh-idle"]["status"] == "implementing"
