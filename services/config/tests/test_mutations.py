"""Failure-injection tests for the sole Config write authority."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.models.schemas import GateRule
from app.store import mutations


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Transaction:
    def __init__(self, conn):
        self.conn = conn
        self.snapshot = None
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        self.snapshot = deepcopy(self.conn.state)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.committed = True
        else:
            self.conn.state = self.snapshot
            self.rolled_back = True
        return False


class FakeConn:
    def __init__(self, owner: str | None = None):
        self.owner = owner
        self.state = {"flags": {}, "experiments": {}, "audits": [], "outbox": []}
        self.last_transaction = None

    def transaction(self):
        self.last_transaction = _Transaction(self)
        return self.last_transaction

    async def fetchrow(self, sql: str, *args):
        if "SELECT key" in sql and "FROM experiments" in sql:
            return {"key": self.owner} if self.owner is not None else None
        raise AssertionError(sql)


class FakePool:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def acquire(self):
        return _Context(self.conn)


class ExposureReceiptConn:
    def __init__(self, existing_payload: dict | None):
        self.existing_payload = existing_payload
        self.insert_sql = ""
        self.select_sql = ""
        self.outbox_sql = ""
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return _Context(None)

    async def fetchrow(self, sql: str, *args):
        if "INSERT INTO config_exposure_receipts" in sql:
            self.insert_sql = sql
            self.calls.append(("receipt", args))
            if self.existing_payload is None:
                self.existing_payload = json.loads(args[2])
                return {"project_id": args[0]}
            return None
        if "SELECT canonical_payload" in sql:
            self.select_sql = sql
            return {"canonical_payload": self.existing_payload}
        raise AssertionError(sql)

    async def execute(self, sql: str, *args):
        if "INSERT INTO config_outbox" not in sql:
            raise AssertionError(sql)
        self.outbox_sql = sql
        self.calls.append(("outbox", args))
        return "INSERT 0 1"


class StrictExperimentTimestampConn:
    """Minimal asyncpg contract double for experiment lifecycle persistence."""

    def __init__(self, row: dict):
        self.row = row
        self.bound_timestamps: list[tuple[datetime | None, datetime | None]] = []

    async def fetchrow(self, sql: str, *args):
        if "FROM experiments" in sql and "FOR UPDATE" in sql:
            return dict(self.row)
        if "UPDATE experiments SET" in sql:
            start_date, end_date = args[13], args[14]
            assert start_date is None or isinstance(start_date, datetime)
            assert end_date is None or isinstance(end_date, datetime)
            self.bound_timestamps.append((start_date, end_date))
            self.row = {
                **self.row,
                "status": args[3],
                "description": args[4],
                "default_variant": args[5],
                "variants_json": args[6],
                "targeting_rules_json": args[7],
                "primary_metric_json": args[8],
                "traffic_percentage": args[10],
                "bucket_by": args[11],
                "minimum_exposure_config_version": args[12],
                "start_date": start_date,
                "end_date": end_date,
                "version": self.row["version"] + 1,
                "updated_at": datetime.now(timezone.utc),
            }
            return dict(self.row)
        raise AssertionError(sql)


def make_flag(overrides: dict | None = None) -> dict:
    flag = {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "state": "active",
        "owners": [],
        "review_by": None,
        "enabled": True,
        "description": "",
        "default_variant": "control",
        "variants": [{"key": "control", "weight": 1}],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
        },
        "salt": "salt",
        "evaluation_mode": "client",
        "auto_disable": False,
        "guardrails": [],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "archived_at": None,
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    if overrides:
        flag.update(overrides)
    return flag


def make_experiment(overrides: dict | None = None) -> dict:
    experiment = {
        "key": "checkout_exp",
        "project_id": "apdl",
        "status": "draft",
        "description": "",
        "flag_key": "checkout",
        "bucket_by": "anonymous_id",
        "default_variant": "control",
        "variants_json": '[{"key":"control","weight":1}]',
        "targeting_rules_json": "[]",
        "primary_metric_json": "{}",
        "statistical_plan": None,
        "traffic_percentage": 100.0,
        "minimum_exposure_config_version": None,
        "start_date": None,
        "end_date": None,
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "archived_at": None,
        "archived_by": None,
    }
    if overrides:
        experiment.update(overrides)
    return experiment


def test_derived_experiment_flag_uses_stored_actor_identity():
    projected = mutations._derived_experiment_flag(
        make_experiment(
            {
                "bucket_by": "anonymous_id",
                "variants_json": (
                    '[{"key":"control","weight":1},'
                    '{"key":"treatment","weight":1}]'
                ),
            }
        ),
        make_flag(),
    )

    assert (
        projected["fallthrough"]["rollout"]["bucket_by"]
        == "anonymous_id"
    )


class NeverPersistConn:
    async def fetchrow(self, sql: str, *args):
        raise AssertionError("invalid flag reached PostgreSQL")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_persistence_boundary_rejects_noncanonical_rollout(operation):
    flag = make_flag(
        {
            "fallthrough": {
                "rollout": {
                    "percentage": "100",
                    "bucket_by": "user_id",
                }
            }
        }
    )
    conn = NeverPersistConn()

    with pytest.raises(ValidationError):
        if operation == "insert":
            await mutations._insert_flag(conn, flag)
        else:
            await mutations._update_flag(conn, flag, flag["version"])


@pytest.mark.asyncio
async def test_persistence_boundary_revalidates_mutated_model_instances():
    rule = GateRule.model_validate(
        {
            "id": "rule",
            "conditions": [],
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"},
        }
    )
    rule.rollout.percentage = "100"
    flag = make_flag({"rules": [rule]})

    with pytest.raises(ValidationError):
        await mutations._insert_flag(NeverPersistConn(), flag)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["audit", "outbox"])
async def test_flag_audit_or_outbox_failure_rolls_back_domain_row(
    monkeypatch,
    failure_stage,
):
    conn = FakeConn()
    pool = FakePool(conn)

    async def insert_flag(current, flag):
        created = make_flag(flag)
        current.state["flags"][created["key"]] = created
        return created

    async def audit_flag(current, **kwargs):
        current.state["audits"].append(kwargs)
        if failure_stage == "audit":
            raise RuntimeError("injected audit failure")

    async def enqueue_flag(current, action, flag):
        current.state["outbox"].append((action, flag["version"]))
        if failure_stage == "outbox":
            raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(mutations, "_insert_flag", insert_flag)
    monkeypatch.setattr(mutations, "_audit_flag", audit_flag)
    monkeypatch.setattr(mutations, "_enqueue_flag_change", enqueue_flag)

    with pytest.raises(RuntimeError, match="injected"):
        await mutations.create_standalone_flag(
            pool,
            make_flag(),
            actor="credential:test",
        )

    assert conn.state == {
        "flags": {},
        "experiments": {},
        "audits": [],
        "outbox": [],
    }
    assert conn.last_transaction.rolled_back is True


@pytest.mark.asyncio
async def test_experiment_outbox_failure_rolls_back_flag_experiment_and_audit(
    monkeypatch,
):
    conn = FakeConn()
    pool = FakePool(conn)

    async def insert_flag(current, flag):
        created = make_flag(flag)
        current.state["flags"][created["key"]] = created
        return created

    async def insert_experiment(current, experiment):
        created = make_experiment(experiment)
        current.state["experiments"][created["key"]] = created
        return created

    async def audit_flag(current, **kwargs):
        current.state["audits"].append(kwargs)

    async def audit_experiment(current, **kwargs):
        current.state["audits"].append(kwargs)

    async def enqueue_flag(current, action, flag):
        current.state["outbox"].append((action, flag["version"]))
        return 7

    async def fail_experiment_outbox(
        current,
        action,
        experiment,
        *,
        project_version,
    ):
        assert project_version == 7
        current.state["outbox"].append((action, experiment["version"]))
        raise RuntimeError("injected experiment outbox failure")

    monkeypatch.setattr(mutations, "_insert_flag", insert_flag)
    monkeypatch.setattr(mutations, "_insert_experiment", insert_experiment)
    monkeypatch.setattr(mutations, "_audit_flag", audit_flag)
    monkeypatch.setattr(mutations, "_audit_experiment", audit_experiment)
    monkeypatch.setattr(mutations, "_enqueue_flag_change", enqueue_flag)
    monkeypatch.setattr(
        mutations,
        "_enqueue_experiment_change",
        fail_experiment_outbox,
    )

    with pytest.raises(RuntimeError, match="injected"):
        await mutations.create_experiment_bundle(
            pool,
            experiment=make_experiment(),
            flag=make_flag(),
            actor="credential:test",
        )

    assert not any(conn.state.values())
    assert conn.last_transaction.rolled_back is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_minimum_version"),
    [("draft", None), ("running", 7)],
)
async def test_experiment_create_stamps_first_analyzable_flag_version(
    monkeypatch,
    status,
    expected_minimum_version,
):
    conn = FakeConn()
    pool = FakePool(conn)
    insert_experiment = AsyncMock(
        side_effect=lambda _conn, experiment: make_experiment(experiment)
    )
    monkeypatch.setattr(
        mutations,
        "_insert_flag",
        AsyncMock(return_value=make_flag({"version": 7})),
    )
    monkeypatch.setattr(mutations, "_insert_experiment", insert_experiment)
    monkeypatch.setattr(mutations, "_audit_flag", AsyncMock())
    monkeypatch.setattr(mutations, "_audit_experiment", AsyncMock())
    monkeypatch.setattr(
        mutations,
        "_enqueue_flag_change",
        AsyncMock(return_value=12),
    )
    monkeypatch.setattr(mutations, "_enqueue_experiment_change", AsyncMock())

    created, _ = await mutations.create_experiment_bundle(
        pool,
        experiment=make_experiment({"status": status}),
        flag=make_flag(),
        actor="credential:test",
    )

    persisted = insert_experiment.await_args.args[1]
    assert persisted["minimum_exposure_config_version"] == expected_minimum_version
    assert created["minimum_exposure_config_version"] == expected_minimum_version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,kwargs",
    [
        (
            mutations.update_standalone_flag,
            {"expected_version": 1, "updates": {"description": "new"}},
        ),
        (
            mutations.transition_standalone_flag,
            {"expected_version": 1, "target_state": "draft"},
        ),
        (
            mutations.disable_standalone_flag,
            {
                "expected_version": 1,
                "reason": "guardrail_failed",
                "evidence": {},
            },
        ),
        (mutations.archive_standalone_flag, {"expected_version": 1}),
        (
            mutations.cleanup_standalone_flag,
            {"expected_version": 1, "evidence": {}},
        ),
    ],
)
async def test_every_generic_flag_mutation_rejects_experiment_ownership(
    monkeypatch,
    command,
    kwargs,
):
    conn = FakeConn(owner="checkout_exp")
    pool = FakePool(conn)
    monkeypatch.setattr(
        mutations,
        "_locked_flag",
        AsyncMock(return_value=make_flag()),
    )

    with pytest.raises(mutations.ExperimentOwnedFlagError) as caught:
        await command(
            pool,
            project_id="apdl",
            key="checkout",
            actor="credential:test",
            **kwargs,
        )

    assert caught.value.experiment_key == "checkout_exp"
    assert conn.last_transaction.rolled_back is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_stale_experiment_version_fails_before_touching_backing_flag(
    monkeypatch,
    operation,
):
    conn = FakeConn()
    pool = FakePool(conn)
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=make_experiment({"version": 4})),
    )
    locked_flag = AsyncMock()
    monkeypatch.setattr(mutations, "_locked_flag", locked_flag)

    with pytest.raises(mutations.VersionConflictError) as caught:
        if operation == "update":
            await mutations.update_experiment_bundle(
                pool,
                desired=make_experiment({"version": 3}),
                expected_version=3,
                actor="credential:test",
            )
        else:
            await mutations.delete_experiment_bundle(
                pool,
                project_id="apdl",
                key="checkout_exp",
                expected_version=3,
                actor="credential:test",
            )

    assert caught.value.current_version == 4
    locked_flag.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "api_field"),
    [
        ("bucket_by", "user_id", "bucket_by"),
        ("default_variant", "treatment", "default_variant"),
        ("traffic_percentage", 50.0, "traffic_percentage"),
        (
            "targeting_rules_json",
            '[{"id":"paid-plan","name":"","conditions":[]}]',
            "targeting_rules",
        ),
    ],
)
async def test_atomic_authority_freezes_analysis_fields_after_draft(
    monkeypatch,
    field,
    value,
    api_field,
):
    conn = FakeConn()
    pool = FakePool(conn)
    existing = make_experiment(
        {
            "status": "running",
            "default_variant": "control",
            "variants_json": (
                '[{"key":"control","weight":1},'
                '{"key":"treatment","weight":1}]'
            ),
            "primary_metric_json": (
                '{"event":"purchase","type":"conversion"}'
            ),
            "start_date": "2026-07-01T00:00:00+00:00",
            "end_date": "2026-08-01T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=existing),
    )
    locked_flag = AsyncMock()
    monkeypatch.setattr(mutations, "_locked_flag", locked_flag)

    with pytest.raises(mutations.ImmutableExperimentError) as caught:
        await mutations.update_experiment_bundle(
            pool,
            desired={**existing, field: value},
            expected_version=1,
            actor="credential:test",
        )

    assert caught.value.fields == [api_field]
    locked_flag.assert_not_awaited()
    assert conn.last_transaction.rolled_back is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_status", "target_status", "end_offset"),
    [
        ("scheduled", "running", timedelta(days=30)),
        ("running", "completed", timedelta(days=-1)),
        ("running", "stopped", timedelta(days=30)),
    ],
)
async def test_experiment_lifecycle_binds_typed_database_timestamps(
    monkeypatch,
    initial_status,
    target_status,
    end_offset,
):
    now = datetime.now(timezone.utc)
    row = make_experiment(
        {
            "status": initial_status,
            "minimum_exposure_config_version": 1,
            "start_date": now - timedelta(days=30),
            "end_date": now + end_offset,
            "created_at": now - timedelta(days=60),
            "updated_at": now - timedelta(days=1),
        }
    )
    conn = StrictExperimentTimestampConn(row)
    desired = {
        **mutations.pg_store._row_to_experiment(row),
        "status": target_status,
    }
    monkeypatch.setattr(
        mutations,
        "_locked_flag",
        AsyncMock(return_value=make_flag()),
    )
    monkeypatch.setattr(
        mutations,
        "_update_flag",
        AsyncMock(return_value=make_flag({"version": 2})),
    )
    monkeypatch.setattr(mutations, "_audit_flag", AsyncMock())
    monkeypatch.setattr(mutations, "_audit_experiment", AsyncMock())
    monkeypatch.setattr(
        mutations,
        "_enqueue_flag_change",
        AsyncMock(return_value=7),
    )
    monkeypatch.setattr(mutations, "_enqueue_experiment_change", AsyncMock())

    updated, _ = await mutations._update_experiment_bundle(
        conn,
        desired=desired,
        expected_version=1,
        actor="credential:test",
        origin="experiment",
    )

    assert updated["status"] == target_status
    assert len(conn.bound_timestamps) == 1
    start_date, end_date = conn.bound_timestamps[0]
    assert isinstance(start_date, datetime)
    assert isinstance(end_date, datetime)
    assert updated["start_date"] is start_date
    assert updated["end_date"] is end_date


@pytest.mark.asyncio
async def test_first_draft_launch_stamps_updated_backing_flag_version(monkeypatch):
    conn = FakeConn()
    before = make_experiment()
    desired = make_experiment(
        {
            "status": "running",
            "start_date": datetime(2026, 7, 1, tzinfo=timezone.utc),
            "end_date": datetime(2026, 8, 1, tzinfo=timezone.utc),
        }
    )
    update_experiment = AsyncMock(
        side_effect=lambda _conn, experiment, _version: {
            **experiment,
            "version": 2,
        }
    )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=before),
    )
    monkeypatch.setattr(
        mutations,
        "_locked_flag",
        AsyncMock(return_value=make_flag({"version": 5})),
    )
    monkeypatch.setattr(
        mutations,
        "_update_flag",
        AsyncMock(return_value=make_flag({"version": 6})),
    )
    monkeypatch.setattr(mutations, "_update_experiment", update_experiment)
    monkeypatch.setattr(mutations, "_audit_flag", AsyncMock())
    monkeypatch.setattr(mutations, "_audit_experiment", AsyncMock())
    monkeypatch.setattr(
        mutations,
        "_enqueue_flag_change",
        AsyncMock(return_value=12),
    )
    monkeypatch.setattr(mutations, "_enqueue_experiment_change", AsyncMock())

    updated, _ = await mutations._update_experiment_bundle(
        conn,
        desired=desired,
        expected_version=1,
        actor="credential:test",
        origin="experiment",
    )

    persisted = update_experiment.await_args.args[1]
    assert persisted["minimum_exposure_config_version"] == 6
    assert updated["minimum_exposure_config_version"] == 6


@pytest.mark.asyncio
async def test_delete_authority_hard_deletes_only_locked_draft(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    experiment = make_experiment({"status": "draft"})
    archived_flag = make_flag(
        {"state": "archived", "enabled": False, "version": 2}
    )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=experiment),
    )
    monkeypatch.setattr(
        mutations,
        "_locked_flag",
        AsyncMock(return_value=make_flag()),
    )
    monkeypatch.setattr(
        mutations,
        "_archive_flag",
        AsyncMock(return_value=archived_flag),
    )
    delete_draft = AsyncMock()
    archive_launched = AsyncMock()
    monkeypatch.setattr(mutations, "_delete_draft_experiment", delete_draft)
    monkeypatch.setattr(mutations, "_archive_experiment", archive_launched)
    audit_experiment = AsyncMock()
    monkeypatch.setattr(mutations, "_audit_experiment", audit_experiment)
    monkeypatch.setattr(mutations, "_audit_flag", AsyncMock())
    monkeypatch.setattr(
        mutations,
        "_enqueue_flag_change",
        AsyncMock(return_value=7),
    )
    enqueue_experiment = AsyncMock()
    monkeypatch.setattr(
        mutations,
        "_enqueue_experiment_change",
        enqueue_experiment,
    )

    removed, _ = await mutations.delete_experiment_bundle(
        pool,
        project_id="apdl",
        key="checkout_exp",
        expected_version=1,
        actor="credential:test",
    )

    delete_draft.assert_awaited_once_with(conn, experiment)
    archive_launched.assert_not_awaited()
    assert removed["archived_at"] is None
    audit_experiment.assert_awaited_once_with(
        conn,
        action="experiment_deleted",
        actor="credential:test",
        before=experiment,
        after=None,
    )
    assert enqueue_experiment.await_args.args[1] == "experiment_deleted"


@pytest.mark.asyncio
async def test_delete_authority_archives_locked_launched_row_after_race(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    launched = make_experiment(
        {
            "status": "running",
            "start_date": "2026-07-01T00:00:00+00:00",
            "end_date": "2026-08-01T00:00:00+00:00",
        }
    )
    archived_at = datetime(2026, 7, 20, tzinfo=timezone.utc)
    archived_experiment = make_experiment(
        {
            "status": "stopped",
            "version": 2,
            "archived_at": archived_at,
            "archived_by": "credential:test",
        }
    )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=launched),
    )
    monkeypatch.setattr(
        mutations,
        "_locked_flag",
        AsyncMock(return_value=make_flag()),
    )
    monkeypatch.setattr(
        mutations,
        "_archive_flag",
        AsyncMock(return_value=make_flag({"version": 2})),
    )
    delete_draft = AsyncMock()
    archive_launched = AsyncMock(return_value=archived_experiment)
    monkeypatch.setattr(mutations, "_delete_draft_experiment", delete_draft)
    monkeypatch.setattr(mutations, "_archive_experiment", archive_launched)
    audit_experiment = AsyncMock()
    monkeypatch.setattr(mutations, "_audit_experiment", audit_experiment)
    monkeypatch.setattr(mutations, "_audit_flag", AsyncMock())
    monkeypatch.setattr(
        mutations,
        "_enqueue_flag_change",
        AsyncMock(return_value=7),
    )
    enqueue_experiment = AsyncMock()
    monkeypatch.setattr(
        mutations,
        "_enqueue_experiment_change",
        enqueue_experiment,
    )

    removed, _ = await mutations.delete_experiment_bundle(
        pool,
        project_id="apdl",
        key="checkout_exp",
        expected_version=1,
        actor="credential:test",
    )

    delete_draft.assert_not_awaited()
    archive_launched.assert_awaited_once_with(
        conn,
        launched,
        actor="credential:test",
    )
    assert removed == archived_experiment
    audit_experiment.assert_awaited_once_with(
        conn,
        action="experiment_archived",
        actor="credential:test",
        before=launched,
        after=archived_experiment,
    )
    assert enqueue_experiment.await_args.args[1] == "experiment_archived"


@pytest.mark.asyncio
async def test_atomic_update_rejects_archived_experiment_before_flag_lock(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    archived = make_experiment(
        {
            "status": "running",
            "archived_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
            "archived_by": "credential:archiver",
        }
    )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=archived),
    )
    locked_flag = AsyncMock()
    monkeypatch.setattr(mutations, "_locked_flag", locked_flag)

    with pytest.raises(mutations.ArchivedExperimentError):
        await mutations.update_experiment_bundle(
            pool,
            desired={**archived, "description": "rewrite"},
            expected_version=1,
            actor="credential:test",
        )

    locked_flag.assert_not_awaited()


def test_atomic_authority_allows_status_only_change_after_draft():
    existing = make_experiment(
        {
            "status": "running",
            "start_date": "2026-07-01T00:00:00+00:00",
            "end_date": "2026-08-01T00:00:00+00:00",
        }
    )

    mutations.ensure_experiment_analysis_fields_immutable(
        existing,
        {**existing, "status": "stopped"},
    )


def test_terminal_transition_persists_actual_analysis_end():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    stop_time = datetime(2026, 7, 10, tzinfo=timezone.utc)
    planned_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    existing = make_experiment(
        {
            "status": "running",
            "start_date": start.isoformat(),
            "end_date": planned_end.isoformat(),
        }
    )

    desired, allowed = mutations.finalize_terminal_analysis_window(
        existing,
        {**existing, "status": "stopped"},
        now=stop_time,
    )

    assert desired["end_date"] == stop_time
    assert allowed == frozenset({"end_date"})
    mutations.ensure_experiment_analysis_fields_immutable(
        existing,
        desired,
        allowed_fields=allowed,
    )


def test_terminal_transition_never_extends_planned_analysis_end():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    planned_end = datetime(2026, 7, 10, tzinfo=timezone.utc)
    existing = make_experiment(
        {
            "status": "running",
            "start_date": start.isoformat(),
            "end_date": planned_end.isoformat(),
        }
    )

    desired, _ = mutations.finalize_terminal_analysis_window(
        existing,
        {**existing, "status": "completed"},
        now=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )

    assert desired["end_date"] == planned_end


def test_fixed_horizon_rejects_early_completed_transition():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    planned_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    existing = make_experiment(
        {
            "status": "running",
            "start_date": start.isoformat(),
            "end_date": planned_end.isoformat(),
        }
    )

    with pytest.raises(mutations.IntegrityError, match="cannot complete before"):
        mutations.finalize_terminal_analysis_window(
            existing,
            {**existing, "status": "completed"},
            now=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("status", ["draft", "scheduled"])
def test_stopped_never_started_experiment_persists_no_analysis_window(status):
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    existing = make_experiment(
        {
            "status": status,
            "start_date": start.isoformat(),
            "end_date": datetime(2026, 8, 10, tzinfo=timezone.utc).isoformat(),
        }
    )

    desired, allowed = mutations.finalize_terminal_analysis_window(
        existing,
        {**existing, "status": "stopped"},
        now=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )

    assert desired["end_date"] is None
    assert allowed == frozenset({"end_date"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,start,end,expected",
    [
        (
            "scheduled",
            "2026-06-01T00:00:00+00:00",
            "2026-07-01T00:00:00+00:00",
            "running",
        ),
        (
            "scheduled",
            "2026-05-01T00:00:00+00:00",
            "2026-06-01T00:00:00+00:00",
            "stopped",
        ),
        (
            "running",
            "2026-05-01T00:00:00+00:00",
            "2026-06-01T00:00:00+00:00",
            "completed",
        ),
    ],
)
async def test_due_lifecycle_transition_is_one_atomic_bundle(
    monkeypatch,
    status,
    start,
    end,
    expected,
):
    conn = FakeConn()
    pool = FakePool(conn)
    experiment = make_experiment(
        {"status": status, "start_date": start, "end_date": end}
    )
    if expected == "running":
        experiment.update(
            {
                "variants_json": (
                    '[{"key":"control","weight":1},'
                    '{"key":"treatment","weight":1}]'
                ),
                "primary_metric_json": (
                    '{"event":"purchase","type":"conversion",'
                    '"direction":"increase"}'
                ),
                "statistical_plan": {
                    "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
                    "baseline_conversion_rate": 0.5,
                    "minimum_detectable_effect": 0.5,
                    "significance_level": 0.05,
                    "nominal_power": 0.8,
                    "required_sample_size_per_arm": 20,
                    "data_settlement_seconds": 5,
                },
            }
        )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=experiment),
    )
    update_bundle = AsyncMock(return_value=(experiment, make_flag()))
    monkeypatch.setattr(mutations, "_update_experiment_bundle", update_bundle)

    await mutations.transition_due_experiment(
        pool,
        project_id="apdl",
        key="checkout_exp",
        expected_version=1,
        now=datetime(2026, 6, 15, tzinfo=timezone.utc),
    )

    assert update_bundle.await_args.kwargs["desired"]["status"] == expected
    assert update_bundle.await_args.kwargs["origin"] == "scheduler"


@pytest.mark.asyncio
async def test_scheduler_refuses_to_start_legacy_experiment_without_plan(monkeypatch):
    conn = FakeConn()
    pool = FakePool(conn)
    experiment = make_experiment(
        {
            "status": "scheduled",
            "start_date": "2026-06-01T00:00:00+00:00",
            "end_date": "2026-07-01T00:00:00+00:00",
            "statistical_plan": None,
        }
    )
    monkeypatch.setattr(
        mutations,
        "_locked_experiment",
        AsyncMock(return_value=experiment),
    )
    update_bundle = AsyncMock()
    monkeypatch.setattr(mutations, "_update_experiment_bundle", update_bundle)

    with pytest.raises(mutations.IntegrityError, match="predeclared statistical plan"):
        await mutations.transition_due_experiment(
            pool,
            project_id="apdl",
            key="checkout_exp",
            expected_version=1,
            now=datetime(2026, 6, 15, tzinfo=timezone.utc),
        )

    update_bundle.assert_not_awaited()


def test_exposure_retry_ignores_generated_event_times():
    first = {
        "stream_key": "events:raw:apdl",
        "event": {
            "message_id": "stable",
            "timestamp": "2026-01-01T00:00:00Z",
            "server_timestamp": "2026-01-01T00:00:00Z",
            "user_id": "user-1",
        },
    }
    retry = deepcopy(first)
    retry["event"]["timestamp"] = "2026-01-01T00:00:01Z"
    retry["event"]["server_timestamp"] = "2026-01-01T00:00:01Z"
    conflict = deepcopy(retry)
    conflict["event"]["user_id"] = "user-2"

    assert mutations._same_exposure_payload(first, retry)
    assert not mutations._same_exposure_payload(first, conflict)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflicting", [False, True])
async def test_exposure_insert_race_rechecks_durable_message_payload(conflicting):
    existing_event = {
        "message_id": "eval_stable_001",
        "timestamp": "2026-07-22T10:00:00Z",
        "server_timestamp": "2026-07-22T10:00:00Z",
        "user_id": "user-1",
    }
    requested_event = {
        **existing_event,
        "timestamp": "2026-07-22T10:00:01Z",
        "server_timestamp": "2026-07-22T10:00:01Z",
    }
    if conflicting:
        requested_event["user_id"] = "user-2"
    existing_payload = mutations._canonical_exposure_payload({
        "stream_key": "events:raw:apdl",
        "event": existing_event,
    })
    conn = ExposureReceiptConn(existing_payload)
    pool = FakePool(conn)

    command = mutations.enqueue_exposure(
        pool,
        project_id="apdl",
        message_id="eval_stable_001",
        stream_key="events:raw:apdl",
        event=requested_event,
    )
    if conflicting:
        with pytest.raises(mutations.IntegrityError, match="was reused"):
            await command
    else:
        await command

    assert "ON CONFLICT (project_id, message_id) DO NOTHING" in conn.insert_sql
    assert "project_id = $1 AND message_id = $2" in conn.select_sql
    assert "FOR UPDATE" in conn.select_sql
    assert "last_seen_at" not in conn.select_sql
    assert conn.outbox_sql == ""


@pytest.mark.asyncio
async def test_new_exposure_receipt_and_delivery_intent_are_created_together():
    conn = ExposureReceiptConn(None)
    pool = FakePool(conn)

    await mutations.enqueue_exposure(
        pool,
        project_id="apdl",
        message_id="eval_new_001",
        stream_key="events:raw:apdl",
        event={
            "message_id": "eval_new_001",
            "timestamp": "2026-07-22T10:00:00Z",
            "server_timestamp": "2026-07-22T10:00:00Z",
            "user_id": "user-1",
        },
    )

    assert [kind for kind, _ in conn.calls] == ["receipt", "outbox"]
    receipt_payload = json.loads(conn.calls[0][1][2])
    outbox_payload = json.loads(conn.calls[1][1][3])
    assert "timestamp" not in receipt_payload["event"]
    assert "server_timestamp" not in receipt_payload["event"]
    assert outbox_payload["event"]["timestamp"] == "2026-07-22T10:00:00Z"
    assert (
        outbox_payload["event"]["server_timestamp"]
        == "2026-07-22T10:00:00Z"
    )


@pytest.mark.asyncio
async def test_exposure_receipt_rejects_a_mismatched_event_message_id():
    conn = ExposureReceiptConn(None)
    pool = FakePool(conn)

    with pytest.raises(mutations.IntegrityError, match="receipt key"):
        await mutations.enqueue_exposure(
            pool,
            project_id="apdl",
            message_id="eval_expected_001",
            stream_key="events:raw:apdl",
            event={
                "message_id": "eval_different_001",
                "timestamp": "2026-07-22T10:00:00Z",
                "server_timestamp": "2026-07-22T10:00:00Z",
                "user_id": "user-1",
            },
        )

    assert conn.calls == []


def test_delivery_payloads_carry_authoritative_versions():
    flag_payload = mutations._flag_delivery(
        "flag_updated",
        make_flag(),
        project_version=12,
    )
    experiment_payload = mutations._experiment_delivery(
        "experiment_updated",
        make_experiment(),
        project_version=12,
    )

    assert flag_payload["project_version"] == 12
    assert flag_payload["data"]["version"] == 1
    assert experiment_payload["project_version"] == 12
    assert experiment_payload["data"]["version"] == 1


@pytest.mark.asyncio
async def test_paired_deliveries_share_one_project_version(monkeypatch):
    next_version = AsyncMock(return_value=23)
    insert_outbox = AsyncMock()
    conn = object()
    monkeypatch.setattr(mutations, "_next_project_version", next_version)
    monkeypatch.setattr(mutations, "_insert_outbox", insert_outbox)

    project_version = await mutations._enqueue_flag_change(
        conn,
        "flag_updated",
        make_flag(),
    )
    await mutations._enqueue_experiment_change(
        conn,
        "experiment_updated",
        make_experiment(),
        project_version=project_version,
    )

    next_version.assert_awaited_once_with(conn, "apdl")
    assert [
        call.kwargs["payload"]["project_version"]
        for call in insert_outbox.await_args_list
    ] == [23, 23]
