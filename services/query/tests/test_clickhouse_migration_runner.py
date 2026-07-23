"""Behavioral tests for the production ClickHouse migration planner."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "pipeline" / "clickhouse" / "migrate.py"
SPEC = importlib.util.spec_from_file_location("apdl_clickhouse_migrate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
migrate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migrate
SPEC.loader.exec_module(migrate)


class _GateFence:
    def __init__(self) -> None:
        self.health_checks = 0

    def assert_held(self) -> None:
        self.health_checks += 1


class _GateClient:
    def __init__(self, *, active_counts: list[int] | None = None) -> None:
        self.calls: list[str] = []
        self.states: list[tuple[int, int]] = []
        self.active_counts = iter(active_counts or [0])
        self.columns = (
            "authority\tString\t1\t1\n"
            "generation\tUInt64\t0\t0\n"
            "writes_blocked\tUInt8\t0\t0\n"
        )
        self.metadata = (
            "ReplacingMergeTree\tauthority\tauthority\t"
            "ReplacingMergeTree(generation) ORDER BY authority "
            "SETTINGS index_granularity = 8192\n"
        )

    def execute(self, sql: str, **_kwargs) -> str:
        self.calls.append(sql)
        if "FROM system.columns" in sql:
            return self.columns
        if "FROM system.tables" in sql:
            return self.metadata
        if "uniqExact(generation)" in sql:
            if not self.states:
                return "0\t0\t0\t0\n"
            generations = {generation for generation, _blocked in self.states}
            generation, blocked = max(self.states)
            return (
                f"{len(self.states)}\t{len(generations)}\t"
                f"{generation}\t{blocked}\n"
            )
        if sql.startswith(f"INSERT INTO {migrate.MAINTENANCE_GATE_TABLE} VALUES"):
            match = re.search(r", ([0-9]+), ([01])\);$", sql)
            assert match is not None
            self.states.append((int(match.group(1)), int(match.group(2))))
            return ""
        if "FROM system.processes" in sql:
            return f"{next(self.active_counts)}\n"
        return ""


class _OwnerClient:
    run_token = "0123456789abcdef0123456789abcdef"

    def __init__(self, *, owner_exists: bool = False) -> None:
        self.owner_exists = owner_exists
        self.calls: list[str] = []

    def execute(self, sql: str, **_kwargs) -> str:
        self.calls.append(sql)
        if sql.lstrip().startswith(
            f"CREATE TABLE {migrate.MAINTENANCE_OWNER_TABLE}"
        ):
            if self.owner_exists:
                raise migrate.subprocess.CalledProcessError(1, ["clickhouse-client"])
            self.owner_exists = True
            return ""
        if "FROM system.columns" in sql:
            return "run_token\tFixedString(32)\n"
        if "SELECT\n    engine," in sql:
            return "TinyLog\t__empty__\t__empty__\n"
        if "uniqExact(run_token)" in sql:
            return f"1\t1\t{self.run_token}\n"
        if sql.startswith(f"DROP TABLE {migrate.MAINTENANCE_OWNER_TABLE}"):
            self.owner_exists = False
            return ""
        if "SELECT count() FROM system.tables" in sql:
            return f"{int(self.owner_exists)}\n"
        return ""


def _write_migration(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql)


def test_discovers_contiguous_checksummed_migrations(tmp_path: Path):
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")

    migrations = migrate.discover_migrations(tmp_path)

    assert [item.name for item in migrations] == [
        "001_first.sql",
        "002_second.sql",
    ]
    assert [item.sql for item in migrations] == ["SELECT 1;\n", "SELECT 2;\n"]
    assert all(len(item.checksum) == 64 for item in migrations)


def test_rejects_a_missing_version_before_connecting(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "003_third.sql", "SELECT 3;\n")

    with pytest.raises(migrate.MigrationError, match="expected 002"):
        migrate.discover_migrations(tmp_path)


def test_applied_ledger_must_be_an_exact_prefix(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    migrations = migrate.discover_migrations(tmp_path)
    first = migrations[0]

    pending = migrate.plan_migrations(
        migrations,
        (migrate.AppliedMigration(first.version, first.name, first.checksum),),
    )

    assert pending == (migrations[1],)


def test_checksum_drift_fails_closed(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    (migration,) = migrate.discover_migrations(tmp_path)

    with pytest.raises(migrate.MigrationError, match="checksum drift"):
        migrate.plan_migrations(
            (migration,),
            (
                migrate.AppliedMigration(
                    migration.version,
                    migration.name,
                    "0" * 64,
                ),
            ),
        )


def test_out_of_order_and_duplicate_ledgers_fail_closed(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    migrations = migrate.discover_migrations(tmp_path)
    first, second = migrations

    with pytest.raises(migrate.MigrationError, match="ordered prefix"):
        migrate.plan_migrations(
            migrations,
            (
                migrate.AppliedMigration(
                    second.version,
                    second.name,
                    second.checksum,
                ),
            ),
        )

    with pytest.raises(migrate.MigrationError, match="duplicate versions"):
        migrate.plan_migrations(
            migrations,
            (
                migrate.AppliedMigration(first.version, first.name, first.checksum),
                migrate.AppliedMigration(
                    first.version,
                    second.name,
                    second.checksum,
                ),
            ),
        )


def test_misplaced_postgres_sql_is_rejected(tmp_path: Path):
    _write_migration(
        tmp_path,
        "001_wrong_engine.sql",
        "-- Target: PostgreSQL\nCREATE EXTENSION IF NOT EXISTS vector;\n",
    )

    with pytest.raises(migrate.MigrationError, match="Misplaced PostgreSQL"):
        migrate.discover_migrations(tmp_path)


def test_prototype_objects_may_only_be_retired(tmp_path: Path):
    _write_migration(
        tmp_path,
        "001_retire_prototype.sql",
        "DROP VIEW IF EXISTS flag_evaluations_v;\n"
        "DROP TABLE IF EXISTS events_v2;\n",
    )
    assert migrate.discover_migrations(tmp_path)

    _write_migration(
        tmp_path,
        "001_retire_prototype.sql",
        "CREATE TABLE events_v2 (project_id String) ENGINE = MergeTree "
        "ORDER BY project_id;\n",
    )
    with pytest.raises(migrate.MigrationError, match="prototype v2"):
        migrate.discover_migrations(tmp_path)


def test_backfills_are_snapshotted_with_canonical_names_and_checksums(
    tmp_path: Path,
):
    _write_migration(tmp_path, "011_identity_aliases.sql", "SELECT 1;\n")

    (backfill,) = migrate.discover_backfills(tmp_path)

    assert backfill.name == "011_identity_aliases.sql"
    assert backfill.sql == "SELECT 1;\n"
    assert len(backfill.checksum) == 64


def test_backfill_names_fail_closed(tmp_path: Path):
    _write_migration(tmp_path, "identity aliases.sql", "SELECT 1;\n")

    with pytest.raises(migrate.MigrationError, match="Invalid ClickHouse backfill"):
        migrate.discover_backfills(tmp_path)


@pytest.mark.parametrize("value", ["0", "901", "not-an-int"])
def test_maintenance_drain_timeout_is_strict(value: str, monkeypatch):
    monkeypatch.setenv("APDL_MAINTENANCE_DRAIN_TIMEOUT_SECONDS", value)

    with pytest.raises(migrate.MigrationError, match="DRAIN_TIMEOUT"):
        migrate._maintenance_timeout_seconds()


@pytest.mark.parametrize("value", ["0", "86401", "not-an-int"])
def test_maintenance_operation_timeout_is_strict(value: str, monkeypatch):
    monkeypatch.setenv("APDL_MAINTENANCE_OPERATION_TIMEOUT_SECONDS", value)

    with pytest.raises(migrate.MigrationError, match="OPERATION_TIMEOUT"):
        migrate._operation_timeout_seconds()


def test_clickhouse_maintenance_uses_the_shared_postgres_authority(monkeypatch):
    monkeypatch.setenv("POSTGRES_CONTAINER_ID", "postgres-container")
    monkeypatch.setenv("POSTGRES_USER", "coordinator")
    monkeypatch.setenv("POSTGRES_DB", "authority")

    assert migrate._maintenance_psql_command() == [
        "docker",
        "exec",
        "-i",
        "postgres-container",
        "psql",
        "-U",
        "coordinator",
        "-d",
        "authority",
    ]


def test_clickhouse_runtime_gate_is_strict_and_brackets_migrations() -> None:
    client = _GateClient()
    fence = _GateFence()

    with migrate._maintenance_writer_gate(client, fence):
        client.calls.append("__SCHEMA_MUTATION__")

    assert client.states == [(1, 1), (2, 0)]
    blocked = next(
        index
        for index, sql in enumerate(client.calls)
        if sql.startswith(f"INSERT INTO {migrate.MAINTENANCE_GATE_TABLE} VALUES")
    )
    killed = next(
        index for index, sql in enumerate(client.calls) if "KILL QUERY" in sql
    )
    mutation = client.calls.index("__SCHEMA_MUTATION__")
    opened = max(
        index
        for index, sql in enumerate(client.calls)
        if sql.startswith(f"INSERT INTO {migrate.MAINTENANCE_GATE_TABLE} VALUES")
    )
    assert blocked < killed < mutation < opened
    assert "startsWith(query_id, 'apdl-runtime-')" in client.calls[killed]
    assert fence.health_checks >= 1


def test_clickhouse_durable_owner_brackets_complete_maintenance() -> None:
    client = _OwnerClient()

    with migrate._maintenance_owner(client, _GateFence()):
        assert client.owner_exists is True
        client.calls.append("__MAINTENANCE_BODY__")

    assert client.owner_exists is False
    create = next(
        index for index, sql in enumerate(client.calls) if "CREATE TABLE" in sql
    )
    body = client.calls.index("__MAINTENANCE_BODY__")
    drop = next(
        index for index, sql in enumerate(client.calls) if sql.startswith("DROP TABLE")
    )
    assert create < body < drop


def test_clickhouse_durable_owner_is_released_after_handled_failure() -> None:
    client = _OwnerClient()

    with pytest.raises(KeyboardInterrupt):
        with migrate._maintenance_owner(client, _GateFence()):
            raise KeyboardInterrupt

    assert client.owner_exists is False


def test_clickhouse_durable_owner_refuses_automatic_crash_takeover() -> None:
    client = _OwnerClient(owner_exists=True)

    with pytest.raises(migrate.MigrationError, match="manual recovery"):
        with migrate._maintenance_owner(client, _GateFence()):
            pytest.fail("stale durable owner must prevent entry")

    assert client.owner_exists is True


def test_clickhouse_runtime_gate_stays_blocked_after_migration_failure() -> None:
    client = _GateClient()

    with pytest.raises(KeyboardInterrupt):
        with migrate._maintenance_writer_gate(client, _GateFence()):
            raise KeyboardInterrupt

    assert client.states == [(1, 1)]


@pytest.mark.parametrize(
    "columns,metadata",
    [
        (
            "authority\tString\t1\t1\n"
            "generation\tUInt64\t0\t0\n"
            "writes_blocked\tBool\t0\t0\n",
            _GateClient().metadata,
        ),
        (
            _GateClient().columns,
            "MergeTree\tauthority\tauthority\tMergeTree ORDER BY authority\n",
        ),
    ],
)
def test_clickhouse_runtime_gate_rejects_competing_schema(
    columns: str,
    metadata: str,
) -> None:
    client = _GateClient()
    client.columns = columns
    client.metadata = metadata

    with pytest.raises(migrate.MigrationError, match="non-canonical"):
        migrate._ensure_maintenance_gate(client, _GateFence())


def test_clickhouse_runtime_gate_retries_prefix_drain_until_absent() -> None:
    client = _GateClient(active_counts=[1, 0])
    fence = _GateFence()

    migrate._drain_writer_queries(client, fence)

    assert sum("KILL QUERY" in sql for sql in client.calls) == 2
    assert sum("FROM system.processes" in sql for sql in client.calls) == 2
    assert fence.health_checks == 2


def test_clickhouse_gate_cannot_open_with_pending_migration(monkeypatch) -> None:
    monkeypatch.setattr(migrate, "discover_migrations", lambda _directory: ("v1",))
    monkeypatch.setattr(migrate, "_read_ledger", lambda _client, _fence: ())
    monkeypatch.setattr(
        migrate,
        "plan_migrations",
        lambda _migrations, _applied: ("v1",),
    )

    with pytest.raises(migrate.MigrationError, match="ledger did not converge"):
        migrate._verify_schema_convergence(
            Path("migrations"),
            (),
            object(),
            _GateFence(),
        )


def test_clickhouse_gate_cannot_open_with_unrecorded_backfill(monkeypatch) -> None:
    backfill = migrate.Backfill("001_seed.sql", "a" * 64, "SELECT 1")
    monkeypatch.setattr(migrate, "discover_migrations", lambda _directory: ())
    monkeypatch.setattr(migrate, "_read_ledger", lambda _client, _fence: ())
    monkeypatch.setattr(migrate, "plan_migrations", lambda *_args: ())
    monkeypatch.setattr(
        migrate,
        "_recorded_backfill_checksum",
        lambda *_args: "",
    )

    with pytest.raises(migrate.MigrationError, match="backfill ledger"):
        migrate._verify_schema_convergence(
            Path("migrations"),
            (backfill,),
            object(),
            _GateFence(),
        )


def test_maintenance_fence_detects_owner_loss_before_apply():
    class LostProcess:
        @staticmethod
        def poll():
            return 1

    fence = migrate.MaintenanceFence(LostProcess(), LostProcess())

    with pytest.raises(migrate.MigrationError, match="owner was lost"):
        fence.assert_held()


def test_clickhouse_cancellation_proves_exact_query_id_is_absent(monkeypatch):
    cancellation_calls = []
    operation_command = []
    events = []
    state = {
        "query_id": "",
        "client_live": True,
        "ambiguous_probe": False,
        "guard_active": False,
    }

    def fake_run(command, **kwargs):
        cancellation_calls.append((command, kwargs))
        command_text = " ".join(command)
        stdout = ""
        if 'cat "$1"' in command_text:
            stdout = "42\n"
        elif "__APDL_PROCESS_ABSENT__" in command_text:
            if state["ambiguous_probe"]:
                state["ambiguous_probe"] = False
                assert state["guard_active"] is True
                events.append("ambiguous-retained")
                return migrate.subprocess.CompletedProcess(command, 2, stdout="")
            stdout = (
                f"clickhouse-client --query_id {state['query_id']}\n"
                if state["client_live"]
                else "__APDL_PROCESS_ABSENT__"
            )
        elif 'kill -"$1"' in command_text:
            signal_name = command[-2]
            events.append(signal_name)
            if signal_name == "KILL":
                state["client_live"] = False
        elif "SELECT count() FROM system.processes" in command_text:
            stdout = "0\n"
        return migrate.subprocess.CompletedProcess(command, 0, stdout=stdout)

    class Fence:
        def assert_held(self):
            events.append("fence-held")

        def run_command(self, command, **kwargs):
            state["guard_active"] = True
            operation_command.extend(command)
            state["query_id"] = command[command.index("--query_id") + 1]
            kwargs["on_started"]()
            events.append("handshake-complete")
            state["ambiguous_probe"] = True

            class CompletedProcess:
                @staticmethod
                def poll():
                    return 0

            completed = migrate.threading.Event()
            completed.set()
            migrate._stop_operation(CompletedProcess(), completed, kwargs["cancel"])
            state["guard_active"] = False
            return ""

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)
    monkeypatch.setattr(migrate, "MAINTENANCE_OPERATION_TERMINATION_SECONDS", 0)
    monkeypatch.setattr(migrate, "MAINTENANCE_CANCELLATION_RETRY_SECONDS", 0)
    client = migrate.ClickHouseClient(
        container_id="clickhouse",
        user="apdl",
        password="secret",
        database="apdl",
    )

    client.execute("SELECT sleep(60)", fence=Fence())

    query_calls = [
        call for call in cancellation_calls if "--query" in call[0]
    ]
    assert len(query_calls) == 2
    kill_command, kill_kwargs = query_calls[0]
    verify_command, verify_kwargs = query_calls[1]
    kill_query = kill_command[kill_command.index("--query") + 1]
    verify_query = verify_command[verify_command.index("--query") + 1]
    query_id = operation_command[operation_command.index("--query_id") + 1]
    assert f"query_id = '{query_id}'" in kill_query
    assert "KILL QUERY" in kill_query
    assert f"query_id = '{query_id}'" in verify_query
    assert "system.processes" in verify_query
    assert kill_kwargs["check"] is True
    assert verify_kwargs["check"] is True
    assert verify_kwargs["timeout"] == migrate.MAINTENANCE_OPERATION_TERMINATION_SECONDS
    assert "printf" in operation_command[operation_command.index("-c") + 1]
    assert events.index("handshake-complete") < events.index("TERM")
    assert events.index("ambiguous-retained") < events.index("TERM")
    assert events.index("TERM") < events.index("KILL")


def test_owner_loss_mid_clickhouse_command_cancels_and_terminates_operation():
    class Fence:
        def __init__(self) -> None:
            self.checks = 0

        def assert_held(self) -> None:
            self.checks += 1
            if self.checks > 1:
                raise migrate.MigrationError("owner lost during ClickHouse apply")

    cancelled: list[bool] = []
    fence = Fence()

    with pytest.raises(migrate.MigrationError, match="lost while"):
        migrate._run_fenced_command(
            fence,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            input_text="",
            capture=False,
            description="applying a ClickHouse migration",
            cancel=lambda: cancelled.append(True),
            heartbeat_seconds=0.01,
            operation_timeout_seconds=1,
        )

    assert cancelled == [True]
    assert fence.checks >= 2
