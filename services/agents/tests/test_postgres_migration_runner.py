"""Behavioral tests for the production PostgreSQL migration planner."""

from __future__ import annotations

import importlib.util
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "pipeline" / "postgres" / "migrate.py"
SPEC = importlib.util.spec_from_file_location("apdl_postgres_migrate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
migrate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migrate
SPEC.loader.exec_module(migrate)


def _write_migration(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql)


def test_discovers_contiguous_migrations_in_order(tmp_path: Path):
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")

    migrations = migrate.discover_migrations(tmp_path)

    assert [item.name for item in migrations] == ["001_first.sql", "002_second.sql"]
    assert all(len(item.checksum) == 64 for item in migrations)


def test_rejects_a_missing_version_before_connecting(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "003_third.sql", "SELECT 3;\n")

    with pytest.raises(migrate.MigrationError, match="expected 002"):
        migrate.discover_migrations(tmp_path)


def test_applied_migrations_are_planned_exactly_once(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    migrations = migrate.discover_migrations(tmp_path)
    first = migrations[0]
    applied = (migrate.AppliedMigration(first.version, first.name, first.checksum),)

    assert migrate.plan_migrations(migrations, applied) == (migrations[1],)
    fully_applied = tuple(
        migrate.AppliedMigration(item.version, item.name, item.checksum)
        for item in migrations
    )
    assert migrate.plan_migrations(migrations, fully_applied) == ()


def test_checksum_drift_fails_closed(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    (migration,) = migrate.discover_migrations(tmp_path)
    applied = (migrate.AppliedMigration(migration.version, migration.name, "0" * 64),)

    with pytest.raises(migrate.MigrationError, match="checksum drift"):
        migrate.plan_migrations((migration,), applied)


def test_out_of_order_ledger_fails_closed(tmp_path: Path):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    _write_migration(tmp_path, "002_second.sql", "SELECT 2;\n")
    migrations = migrate.discover_migrations(tmp_path)
    second = migrations[1]
    applied = (migrate.AppliedMigration(second.version, second.name, second.checksum),)

    with pytest.raises(migrate.MigrationError, match="ordered prefix"):
        migrate.plan_migrations(migrations, applied)


def test_empty_ledger_accepts_only_an_empty_public_schema(monkeypatch):
    calls: list[tuple[str, bool]] = []

    def fake_psql(sql: str, fence, *, variables=None, capture: bool = False):
        del fence, variables
        calls.append((sql, capture))
        return ""

    monkeypatch.setattr(migrate, "_psql", fake_psql)

    migrate._assert_fresh_database_for_empty_ledger((), object())

    assert len(calls) == 1
    assert calls[0][1] is True
    assert "pg_catalog.pg_tables" in calls[0][0]
    assert "tablename <> 'apdl_schema_migrations'" in calls[0][0]


def test_empty_ledger_rejects_unversioned_public_tables(monkeypatch):
    monkeypatch.setattr(
        migrate,
        "_psql",
        lambda *args, **kwargs: "experiments\nflags\n",
    )

    with pytest.raises(
        migrate.MigrationError,
        match="Fresh-install-only release found public tables.*experiments, flags",
    ):
        migrate._assert_fresh_database_for_empty_ledger((), object())


def test_existing_ledger_prefix_skips_fresh_database_preflight(monkeypatch):
    migration = migrate.AppliedMigration(1, "001_auth.sql", "a" * 64)

    def unexpected_psql(*args, **kwargs):
        raise AssertionError("fresh-install preflight must not run for a ledger prefix")

    monkeypatch.setattr(migrate, "_psql", unexpected_psql)

    migrate._assert_fresh_database_for_empty_ledger((migration,), object())


def test_migrate_holds_exclusive_fence_across_every_schema_operation(
    tmp_path: Path,
    monkeypatch,
):
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;\n")
    events: list[str] = []

    @contextmanager
    def fence():
        events.append("fence-enter")
        class Fence:
            pass

        yield Fence()
        events.append("fence-exit")

    monkeypatch.setattr(migrate, "_wait_for_postgres", lambda: events.append("ready"))
    monkeypatch.setattr(migrate, "_maintenance_fence", fence)
    monkeypatch.setattr(
        migrate,
        "_ensure_ledger",
        lambda _fence: events.append("ledger"),
    )
    monkeypatch.setattr(migrate, "_read_ledger", lambda _fence: ())
    monkeypatch.setattr(
        migrate,
        "_assert_fresh_database_for_empty_ledger",
        lambda _applied, _fence: events.append("fresh-check"),
    )
    monkeypatch.setattr(
        migrate,
        "_apply_migration",
        lambda migration, _fence: events.append(f"apply:{migration.name}"),
    )

    migrate.migrate(tmp_path)

    assert events == [
        "ready",
        "fence-enter",
        "ledger",
        "fresh-check",
        "apply:001_first.sql",
        "fence-exit",
    ]


def test_maintenance_fence_detects_owner_loss_before_apply():
    class LostProcess:
        @staticmethod
        def poll():
            return 1

    fence = migrate.MaintenanceFence(LostProcess(), LostProcess())

    with pytest.raises(migrate.MigrationError, match="owner was lost"):
        fence.assert_held()


def test_postgres_operation_registers_bounded_server_side_termination(monkeypatch):
    cancellation_calls = []

    def fake_run(command, **kwargs):
        cancellation_calls.append((command, kwargs))
        return migrate.subprocess.CompletedProcess(
            command,
            0,
            stdout="t\nt\n",
        )

    class Fence:
        def run_command(self, command, **kwargs):
            assert command[0] == "psql"
            operation_name = kwargs["environment"]["PGAPPNAME"]
            assert operation_name.startswith("apdl-migration-")
            kwargs["cancel"]()
            return ""

    monkeypatch.setattr(migrate.subprocess, "run", fake_run)

    migrate._psql("SELECT 1;", Fence())

    assert len(cancellation_calls) == 1
    command, kwargs = cancellation_calls[0]
    operation_variable = next(
        item for item in command if item.startswith("operation_application_name=")
    )
    assert operation_variable.removeprefix("operation_application_name=").startswith(
        "apdl-migration-"
    )
    assert "pg_terminate_backend" in kwargs["input"]
    assert "count(*) = 0" in kwargs["input"]
    assert kwargs["timeout"] == migrate.MAINTENANCE_OPERATION_TERMINATION_SECONDS


def test_owner_loss_mid_command_terminates_operation_before_returning():
    class Fence:
        def __init__(self) -> None:
            self.checks = 0

        def assert_held(self) -> None:
            self.checks += 1
            if self.checks > 1:
                raise migrate.MigrationError("owner lost during apply")

    cancelled: list[bool] = []
    fence = Fence()

    with pytest.raises(migrate.MigrationError, match="lost while"):
        migrate._run_fenced_command(
            fence,
            [sys.executable, "-c", "import time; time.sleep(60)"],
            input_text="",
            capture=False,
            description="applying a PostgreSQL migration",
            cancel=lambda: cancelled.append(True),
            heartbeat_seconds=0.01,
            operation_timeout_seconds=1,
        )

    assert cancelled == [True]
    assert fence.checks >= 2


def test_launcher_is_reaped_before_server_cancellation() -> None:
    completed = threading.Event()
    order: list[str] = []

    class Process:
        terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self) -> None:
            order.append("terminate-launcher")
            self.terminated = True
            completed.set()

    process = Process()

    def cancel_server_operation() -> None:
        assert process.terminated is True
        order.append("prove-server-absence")

    migrate._stop_operation(process, completed, cancel_server_operation)

    assert order == ["terminate-launcher", "prove-server-absence"]


def test_keyboard_interrupt_still_cancels_and_proves_server_absence(monkeypatch):
    real_event = threading.Event

    class InterruptingEvent:
        def __init__(self) -> None:
            self.inner = real_event()
            self.interrupted = False

        def set(self) -> None:
            self.inner.set()

        def wait(self, timeout=None):
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return self.inner.wait(timeout)

    class ThreadingFacade:
        Event = InterruptingEvent
        Thread = threading.Thread
        current_thread = staticmethod(threading.current_thread)
        main_thread = staticmethod(threading.main_thread)

    class Fence:
        def assert_held(self) -> None:
            return None

    cancellations = []
    monkeypatch.setattr(migrate, "threading", ThreadingFacade)

    with pytest.raises(KeyboardInterrupt):
        migrate._run_fenced_command(
            Fence(),
            [sys.executable, "-c", "import time; time.sleep(60)"],
            input_text="",
            capture=False,
            description="applying a PostgreSQL migration",
            cancel=lambda: cancellations.append("server-absent"),
            heartbeat_seconds=0.01,
            operation_timeout_seconds=1,
        )

    assert cancellations == ["server-absent"]


def test_sigterm_is_deferred_until_child_and_server_are_stopped(monkeypatch):
    handlers = {}

    def fake_getsignal(signum):
        return f"previous-{signum}"

    def fake_signal(signum, handler):
        handlers[signum] = handler

    class Fence:
        checks = 0

        def assert_held(self) -> None:
            self.checks += 1
            if self.checks == 2:
                handlers[migrate.signal.SIGTERM](migrate.signal.SIGTERM, None)

    cancellations = []
    monkeypatch.setattr(migrate.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(migrate.signal, "signal", fake_signal)

    with pytest.raises(migrate.MaintenanceTerminationRequested):
        migrate._run_fenced_command(
            Fence(),
            [sys.executable, "-c", "import time; time.sleep(60)"],
            input_text="",
            capture=False,
            description="applying a PostgreSQL migration",
            cancel=lambda: cancellations.append("server-absent"),
            heartbeat_seconds=0.01,
            operation_timeout_seconds=1,
        )

    assert cancellations == ["server-absent"]
    assert handlers[migrate.signal.SIGINT] == f"previous-{migrate.signal.SIGINT}"
    assert handlers[migrate.signal.SIGTERM] == f"previous-{migrate.signal.SIGTERM}"


def test_communication_base_exception_still_proves_server_absence(monkeypatch):
    class Process:
        returncode = 1

        @staticmethod
        def communicate(*, input):
            del input
            raise SystemExit("communication failed")

        @staticmethod
        def poll():
            return 1

    class Fence:
        def assert_held(self) -> None:
            return None

    cancellations = []
    monkeypatch.setattr(migrate.subprocess, "Popen", lambda *_args, **_kwargs: Process())

    with pytest.raises(SystemExit, match="communication failed"):
        migrate._run_fenced_command(
            Fence(),
            ["psql"],
            input_text="SELECT 1;",
            capture=False,
            description="applying a PostgreSQL migration",
            cancel=lambda: cancellations.append("server-absent"),
            heartbeat_seconds=0.01,
            operation_timeout_seconds=1,
        )

    assert cancellations == ["server-absent"]


def test_unproven_backend_termination_keeps_guard_context_live(monkeypatch):
    class Fence:
        def __init__(self) -> None:
            self.checks = 0

        def assert_held(self) -> None:
            self.checks += 1
            if self.checks > 1:
                raise migrate.MigrationError("owner lost during apply")

    cancellation_attempted = threading.Event()
    allow_cancellation_proof = threading.Event()
    guard_active = threading.Event()
    cancellations = [0]
    errors: list[BaseException] = []

    def fail_cancellation() -> None:
        cancellations[0] += 1
        cancellation_attempted.set()
        if not allow_cancellation_proof.is_set():
            raise migrate.MigrationError("backend termination not confirmed")

    @contextmanager
    def guarded_fence():
        guard_active.set()
        try:
            yield Fence()
        finally:
            guard_active.clear()

    def run_operation() -> None:
        try:
            with guarded_fence() as fence:
                migrate._run_fenced_command(
                    fence,
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    input_text="",
                    capture=False,
                    description="applying a PostgreSQL migration",
                    cancel=fail_cancellation,
                    heartbeat_seconds=0.01,
                    operation_timeout_seconds=1,
                )
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(migrate, "MAINTENANCE_CANCELLATION_RETRY_SECONDS", 0.001)
    operation = threading.Thread(target=run_operation, daemon=True)
    operation.start()

    assert cancellation_attempted.wait(1)
    assert guard_active.is_set()
    assert operation.is_alive()

    allow_cancellation_proof.set()
    operation.join(2)

    assert not operation.is_alive()
    assert not guard_active.is_set()
    assert cancellations[0] >= 2
    assert len(errors) == 1
    assert "ownership was lost" in str(errors[0])


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
