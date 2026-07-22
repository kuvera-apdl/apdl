#!/usr/bin/env python3
"""Apply the immutable APDL PostgreSQL migration sequence."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator


MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")
LEDGER_TABLE = "apdl_schema_migrations"
ADVISORY_LOCK_ID = 4_158_044_082
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084
DEFAULT_MAINTENANCE_DRAIN_TIMEOUT_SECONDS = 30
DEFAULT_MAINTENANCE_OPERATION_TIMEOUT_SECONDS = 3_600
MAINTENANCE_FENCE_MARKER = "__APDL_MAINTENANCE_FENCE_ACQUIRED__"
MAINTENANCE_HEALTH_MARKER = "__APDL_MAINTENANCE_FENCE_HEALTHY__"
MAINTENANCE_HEALTH_TIMEOUT_SECONDS = 5
MAINTENANCE_OPERATION_HEARTBEAT_SECONDS = 0.25
MAINTENANCE_OPERATION_TERMINATION_SECONDS = 5
MAINTENANCE_CANCELLATION_RETRY_SECONDS = 1.0
POSTGRES_BACKEND_TERMINATION_TIMEOUT_MS = 3_000


class MigrationError(RuntimeError):
    """The on-disk sequence or persisted migration ledger is invalid."""


class MaintenanceTerminationRequested(BaseException):
    """A catchable process termination request received during fenced work."""


@contextmanager
def _defer_sigterm_until_operation_is_safe() -> Iterator[Callable[[], None]]:
    requested = [False]

    def raise_if_requested() -> None:
        if requested[0]:
            raise MaintenanceTerminationRequested

    if threading.current_thread() is not threading.main_thread():
        yield raise_if_requested
        return
    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def request_termination(_signum, _frame) -> None:
        requested[0] = True

    for signum in previous_handlers:
        signal.signal(signum, request_termination)
    try:
        yield raise_if_requested
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    path: Path


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


def _read_fence_response(
    process: subprocess.Popen[str],
    marker: str,
    *,
    timeout_seconds: int,
) -> list[str]:
    """Read one bounded psql response, killing an unresponsive fence owner."""
    assert process.stdout is not None
    timed_out = threading.Event()

    def terminate_unresponsive_owner() -> None:
        timed_out.set()
        if process.poll() is None:
            process.kill()

    timer = threading.Timer(timeout_seconds, terminate_unresponsive_owner)
    timer.start()
    output: list[str] = []
    found_marker = False
    try:
        for line in process.stdout:
            normalized = line.rstrip("\n")
            if normalized == marker:
                found_marker = True
                break
            output.append(normalized)
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise MigrationError("The APDL maintenance inhibitor owner stopped responding")
    if not found_marker:
        raise MigrationError("The APDL maintenance inhibitor owner exited unexpectedly")
    return output


@dataclass(frozen=True)
class MaintenanceFence:
    """Live handles for redundant PostgreSQL exclusive maintenance barriers."""

    process: subprocess.Popen[str]
    guard_process: subprocess.Popen[str]

    @staticmethod
    def _assert_owner(
        process: subprocess.Popen[str],
        owner: str,
        lock_id: int,
    ) -> None:
        if process.poll() is not None:
            raise MigrationError(f"The APDL maintenance {owner} owner was lost")
        assert process.stdin is not None
        process.stdin.write(
            "SELECT count(*) = 1 FROM pg_catalog.pg_locks "
            "WHERE pid = pg_backend_pid() "
            "AND locktype = 'advisory' "
            "AND mode = 'ExclusiveLock' AND granted "
            "AND classid = 0 AND objsubid = 1 "
            f"AND objid = ({lock_id}::bigint)::oid;\n"
            f"\\echo {MAINTENANCE_HEALTH_MARKER}\n"
        )
        try:
            process.stdin.flush()
        except BrokenPipeError as exc:
            raise MigrationError(
                f"The APDL maintenance {owner} owner was lost"
            ) from exc
        output = _read_fence_response(
            process,
            MAINTENANCE_HEALTH_MARKER,
            timeout_seconds=MAINTENANCE_HEALTH_TIMEOUT_SECONDS,
        )
        if [line for line in output if line] != ["t"]:
            raise MigrationError(f"The exclusive APDL maintenance {owner} was lost")

    def assert_held(self) -> None:
        self._assert_owner(
            self.process,
            "inhibitor",
            MAINTENANCE_INHIBITOR_LOCK_ID,
        )
        self._assert_owner(
            self.guard_process,
            "guard",
            MAINTENANCE_GUARD_LOCK_ID,
        )

    def run_command(
        self,
        command: list[str],
        *,
        input_text: str,
        capture: bool,
        description: str,
        environment: dict[str, str] | None = None,
        cancel: Callable[[], None] | None = None,
        heartbeat_seconds: float = MAINTENANCE_OPERATION_HEARTBEAT_SECONDS,
        operation_timeout_seconds: int | None = None,
    ) -> str:
        """Run one operation while actively supervising both fence owners."""
        return _run_fenced_command(
            self,
            command,
            input_text=input_text,
            capture=capture,
            description=description,
            environment=environment,
            cancel=cancel,
            heartbeat_seconds=heartbeat_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )


def _operation_timeout_seconds() -> int:
    raw_value = os.environ.get(
        "APDL_MAINTENANCE_OPERATION_TIMEOUT_SECONDS",
        str(DEFAULT_MAINTENANCE_OPERATION_TIMEOUT_SECONDS),
    )
    try:
        timeout = int(raw_value)
    except ValueError as exc:
        raise MigrationError(
            "APDL_MAINTENANCE_OPERATION_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if timeout < 1 or timeout > 86_400:
        raise MigrationError(
            "APDL_MAINTENANCE_OPERATION_TIMEOUT_SECONDS must be between 1 and 86400"
        )
    return timeout


def _stop_operation(
    process: subprocess.Popen[str],
    completed: threading.Event,
    cancel: Callable[[], None] | None,
) -> None:
    """Cancel and reap an in-flight command before the surviving guard is released."""
    warned_about_launcher = False
    while True:
        try:
            if process.poll() is None:
                process.terminate()
            if process.poll() is not None:
                break
            completed.wait(MAINTENANCE_OPERATION_TERMINATION_SECONDS)
            if process.poll() is not None:
                break
            if process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
        except BaseException:
            continue
        if not warned_about_launcher:
            print(
                "CRITICAL: local migration client termination is unproven; "
                "retaining the maintenance guard and retrying",
                file=sys.stderr,
                flush=True,
            )
            warned_about_launcher = True
        try:
            time.sleep(MAINTENANCE_CANCELLATION_RETRY_SECONDS)
        except BaseException:
            pass

    if cancel is None:
        raise MigrationError(
            "Migration operation has no server-side cancellation proof"
        )
    warned_about_server = False
    while True:
        try:
            cancel()
            break
        except BaseException:
            if not warned_about_server:
                print(
                    "CRITICAL: database operation termination is unproven; "
                    "retaining the maintenance guard and retrying",
                    file=sys.stderr,
                    flush=True,
                )
                warned_about_server = True
            try:
                time.sleep(MAINTENANCE_CANCELLATION_RETRY_SECONDS)
            except BaseException:
                pass


def _run_fenced_command(
    fence: MaintenanceFence,
    command: list[str],
    *,
    input_text: str,
    capture: bool,
    description: str,
    environment: dict[str, str] | None = None,
    cancel: Callable[[], None] | None = None,
    heartbeat_seconds: float = MAINTENANCE_OPERATION_HEARTBEAT_SECONDS,
    operation_timeout_seconds: int | None = None,
) -> str:
    with _defer_sigterm_until_operation_is_safe() as termination_check:
        return _run_fenced_command_impl(
            fence,
            command,
            input_text=input_text,
            capture=capture,
            description=description,
            environment=environment,
            cancel=cancel,
            termination_check=termination_check,
            heartbeat_seconds=heartbeat_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        )


def _run_fenced_command_impl(
    fence: MaintenanceFence,
    command: list[str],
    *,
    input_text: str,
    capture: bool,
    description: str,
    termination_check: Callable[[], None],
    environment: dict[str, str] | None = None,
    cancel: Callable[[], None] | None = None,
    heartbeat_seconds: float = MAINTENANCE_OPERATION_HEARTBEAT_SECONDS,
    operation_timeout_seconds: int | None = None,
) -> str:
    """Continuously prove fence ownership while a child command is active."""
    if cancel is None:
        raise MigrationError(
            "A fenced migration operation requires server-side cancellation"
        )
    fence.assert_held()
    timeout_seconds = (
        _operation_timeout_seconds()
        if operation_timeout_seconds is None
        else operation_timeout_seconds
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        text=True,
        env=environment,
    )
    completed = threading.Event()
    output: list[str] = [""]
    communication_error: list[BaseException] = []

    def communicate() -> None:
        try:
            stdout, _ = process.communicate(input=input_text)
            output[0] = stdout or ""
        except BaseException as exc:
            communication_error.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(
        target=communicate,
        name="apdl-migration-operation",
        daemon=True,
    )
    try:
        termination_check()
        thread.start()
        termination_check()
        deadline = time.monotonic() + timeout_seconds
        while not completed.wait(heartbeat_seconds):
            termination_check()
            if time.monotonic() >= deadline:
                raise MigrationError(
                    f"Timed out while {description} after {timeout_seconds} seconds"
                )
            try:
                fence.assert_held()
            except BaseException as exc:
                raise MigrationError(
                    f"Maintenance fence ownership was lost while {description}"
                ) from exc

        termination_check()
        fence.assert_held()
        if communication_error:
            raise communication_error[0]
        if process.returncode != 0:
            raise subprocess.CalledProcessError(process.returncode, command)
        return output[0]
    except BaseException:
        _stop_operation(process, completed, cancel)
        raise


def discover_migrations(directory: Path) -> tuple[Migration, ...]:
    """Return a contiguous, checksummed migration sequence."""
    if not directory.is_dir():
        raise MigrationError(f"PostgreSQL migrations directory not found: {directory}")

    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise MigrationError(f"No PostgreSQL migrations found in: {directory}")

    migrations: list[Migration] = []
    for expected_version, path in enumerate(paths, start=1):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"Invalid PostgreSQL migration name: {path.name}")
        version = int(match.group("version"))
        if version != expected_version:
            raise MigrationError(
                "PostgreSQL migrations must be contiguous from 001; "
                f"expected {expected_version:03d}, found {path.name}"
            )
        migrations.append(
            Migration(
                version=version,
                name=path.name,
                checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
                path=path,
            )
        )
    return tuple(migrations)


def plan_migrations(
    migrations: Iterable[Migration],
    applied: Iterable[AppliedMigration],
) -> tuple[Migration, ...]:
    """Validate that the ledger is an exact prefix and return pending files."""
    migration_sequence = tuple(migrations)
    applied_sequence = tuple(sorted(applied, key=lambda item: item.version))

    if len({item.version for item in applied_sequence}) != len(applied_sequence):
        raise MigrationError("Migration ledger contains duplicate versions")
    if len({item.name for item in applied_sequence}) != len(applied_sequence):
        raise MigrationError("Migration ledger contains duplicate names")
    if len(applied_sequence) > len(migration_sequence):
        raise MigrationError(
            "Migration ledger references files absent from this release"
        )

    for index, applied_item in enumerate(applied_sequence):
        expected = migration_sequence[index]
        if applied_item.version != expected.version:
            raise MigrationError(
                "Migration ledger is not an ordered prefix: "
                f"expected version {expected.version:03d}, found {applied_item.version:03d}"
            )
        if applied_item.name != expected.name:
            raise MigrationError(
                f"Migration {expected.version:03d} name drift: "
                f"ledger={applied_item.name}, file={expected.name}"
            )
        if applied_item.checksum != expected.checksum:
            raise MigrationError(
                f"Migration {expected.name} checksum drift: "
                f"ledger={applied_item.checksum}, file={expected.checksum}"
            )

    return migration_sequence[len(applied_sequence) :]


def _maintenance_timeout_seconds() -> int:
    raw_value = os.environ.get(
        "APDL_MAINTENANCE_DRAIN_TIMEOUT_SECONDS",
        str(DEFAULT_MAINTENANCE_DRAIN_TIMEOUT_SECONDS),
    )
    try:
        timeout = int(raw_value)
    except ValueError as exc:
        raise MigrationError(
            "APDL_MAINTENANCE_DRAIN_TIMEOUT_SECONDS must be an integer"
        ) from exc
    if timeout < 1 or timeout > 900:
        raise MigrationError(
            "APDL_MAINTENANCE_DRAIN_TIMEOUT_SECONDS must be between 1 and 900"
        )
    return timeout


@contextmanager
def _maintenance_fence() -> Iterator[MaintenanceFence]:
    """Hold redundant exclusive barriers for the complete migration interval."""
    timeout_seconds = _maintenance_timeout_seconds()
    owners: list[tuple[subprocess.Popen[str], int]] = []

    def acquire_owner(lock_id: int) -> subprocess.Popen[str]:
        command = [
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
        ]
        environment = os.environ.copy()
        environment.setdefault("PGCONNECT_TIMEOUT", "5")
        owner = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=environment,
        )
        assert owner.stdin is not None
        assert owner.stdout is not None
        owner.stdin.write(
            f"SET lock_timeout TO '{timeout_seconds * 1000}ms';\n"
            f"SELECT pg_advisory_lock({lock_id});\n"
            f"\\echo {MAINTENANCE_FENCE_MARKER}\n"
        )
        owner.stdin.flush()
        try:
            _read_fence_response(
                owner,
                MAINTENANCE_FENCE_MARKER,
                timeout_seconds=timeout_seconds + 5,
            )
        except MigrationError:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
            raise
        owners.append((owner, lock_id))
        return owner

    def release_owner(owner: subprocess.Popen[str], lock_id: int) -> None:
        if owner.poll() is not None:
            return
        assert owner.stdin is not None
        try:
            owner.stdin.write(f"SELECT pg_advisory_unlock({lock_id});\n\\q\n")
            owner.stdin.flush()
            owner.communicate(timeout=5)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            owner.kill()
            owner.wait(timeout=5)

    try:
        process = acquire_owner(MAINTENANCE_INHIBITOR_LOCK_ID)
        guard_process = acquire_owner(MAINTENANCE_GUARD_LOCK_ID)
        fence = MaintenanceFence(process, guard_process)
        fence.assert_held()
        yield fence
    except MigrationError as exc:
        if len(owners) < 2:
            raise MigrationError(
                "Could not acquire the exclusive APDL maintenance inhibitor; "
                "running services did not drain before the deadline"
            ) from exc
        raise
    finally:
        for owner, lock_id in reversed(owners):
            release_owner(owner, lock_id)


def _psql(
    sql: str,
    fence: MaintenanceFence,
    *,
    variables: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    command = ["psql", "-X", "-v", "ON_ERROR_STOP=1"]
    if capture:
        command.extend(("-A", "-t", "-F", "|"))
    for key, value in (variables or {}).items():
        command.extend(("-v", f"{key}={value}"))
    operation_name = f"apdl-migration-{uuid.uuid4().hex}"
    environment = os.environ.copy()
    environment["PGAPPNAME"] = operation_name
    environment.setdefault("PGCONNECT_TIMEOUT", "5")

    def cancel_operation() -> None:
        cancel_environment = os.environ.copy()
        cancel_environment["PGAPPNAME"] = "apdl-migration-canceller"
        cancel_environment.setdefault("PGCONNECT_TIMEOUT", "5")
        result = subprocess.run(
            [
                "psql",
                "-X",
                "-q",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"operation_application_name={operation_name}",
            ],
            input=(
                "SELECT COALESCE(bool_and(pg_terminate_backend(pid, "
                f"{POSTGRES_BACKEND_TERMINATION_TIMEOUT_MS})), TRUE) "
                "FROM pg_catalog.pg_stat_activity "
                "WHERE datname = current_database() "
                "AND usename = current_user "
                "AND application_name = :'operation_application_name' "
                "AND pid <> pg_backend_pid();\n"
                "SELECT count(*) = 0 FROM pg_catalog.pg_stat_activity "
                "WHERE datname = current_database() "
                "AND usename = current_user "
                "AND application_name = :'operation_application_name';\n"
            ),
            text=True,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=cancel_environment,
            timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
        )
        output = [line for line in result.stdout.splitlines() if line]
        if result.returncode != 0 or output != ["t", "t"]:
            raise MigrationError(
                "PostgreSQL did not confirm migration backend termination"
            )

    return fence.run_command(
        command,
        input_text=sql,
        capture=capture,
        description="running a PostgreSQL migration operation",
        environment=environment,
        cancel=cancel_operation,
    )


def _wait_for_postgres() -> None:
    for _ in range(30):
        result = subprocess.run(
            ["pg_isready"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise MigrationError("PostgreSQL did not become ready in time")


def _ensure_ledger(fence: MaintenanceFence) -> None:
    _psql(
        f"""
BEGIN;
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_ID});
CREATE TABLE IF NOT EXISTS public.{LEDGER_TABLE} (
    version INTEGER PRIMARY KEY CHECK (version BETWEEN 1 AND 999),
    name TEXT NOT NULL UNIQUE
        CHECK (name ~ '^[0-9]{{3}}_[a-z0-9_]+\\.sql$'),
    checksum CHAR(64) NOT NULL
        CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.apdl_reject_migration_ledger_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $immutable_ledger$
BEGIN
    RAISE EXCEPTION 'apdl_schema_migrations rows are immutable';
END
$immutable_ledger$;

DO $install_immutable_triggers$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'public.apdl_schema_migrations'::regclass
          AND tgname = 'apdl_schema_migrations_no_update_delete'
    ) THEN
        CREATE TRIGGER apdl_schema_migrations_no_update_delete
        BEFORE UPDATE OR DELETE ON public.apdl_schema_migrations
        FOR EACH ROW EXECUTE FUNCTION public.apdl_reject_migration_ledger_mutation();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'public.apdl_schema_migrations'::regclass
          AND tgname = 'apdl_schema_migrations_no_truncate'
    ) THEN
        CREATE TRIGGER apdl_schema_migrations_no_truncate
        BEFORE TRUNCATE ON public.apdl_schema_migrations
        FOR EACH STATEMENT EXECUTE FUNCTION public.apdl_reject_migration_ledger_mutation();
    END IF;
END
$install_immutable_triggers$;
COMMIT;
""",
        fence,
    )


def _read_ledger(fence: MaintenanceFence) -> tuple[AppliedMigration, ...]:
    output = _psql(
        f"""
SELECT version, name, checksum
FROM public.{LEDGER_TABLE}
ORDER BY version;
""",
        fence,
        capture=True,
    )
    applied: list[AppliedMigration] = []
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("|")
        if len(fields) != 3:
            raise MigrationError(f"Could not parse migration ledger row: {line!r}")
        applied.append(
            AppliedMigration(
                version=int(fields[0]),
                name=fields[1],
                checksum=fields[2],
            )
        )
    return tuple(applied)


def _assert_fresh_database_for_empty_ledger(
    applied: tuple[AppliedMigration, ...],
    fence: MaintenanceFence,
) -> None:
    """Refuse to adopt an unversioned APDL schema.

    The developer-preview release supports fresh databases and exact-prefix
    migrations created by this ledger only.  In particular, migration 006
    contains legacy reconciliation that cannot prove the original meaning of
    every pre-ledger flag row.  An empty ledger beside existing public tables
    is therefore an unsupported in-place upgrade, not a fresh install.
    """
    if applied:
        return

    output = _psql(
        f"""
SELECT tablename
FROM pg_catalog.pg_tables
WHERE schemaname = 'public'
  AND tablename <> '{LEDGER_TABLE}'
ORDER BY tablename;
""",
        fence,
        capture=True,
    )
    existing_tables = tuple(line.strip() for line in output.splitlines() if line.strip())
    if existing_tables:
        joined = ", ".join(existing_tables)
        raise MigrationError(
            "Fresh-install-only release found public tables without an APDL "
            f"migration ledger: {joined}. Start with an empty database; "
            "in-place legacy upgrades are unsupported."
        )


def _apply_migration(migration: Migration, fence: MaintenanceFence) -> None:
    # The lock and second ledger check make two concurrent migrators safe even
    # when both produced their plan from the same earlier ledger snapshot.
    wrapper = f"""
BEGIN;
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_ID});
SELECT
    EXISTS (
        SELECT 1 FROM public.{LEDGER_TABLE}
        WHERE version = :migration_version
          AND (name <> :'migration_name' OR checksum <> :'migration_checksum')
    ) AS migration_drift,
    EXISTS (
        SELECT 1 FROM public.{LEDGER_TABLE}
        WHERE version = :migration_version
          AND name = :'migration_name'
          AND checksum = :'migration_checksum'
    ) AS migration_applied,
    (
        NOT EXISTS (
            SELECT 1 FROM public.{LEDGER_TABLE}
            WHERE version = :migration_version
        )
        AND EXISTS (
            SELECT 1 FROM public.{LEDGER_TABLE}
            WHERE version > :migration_version
        )
    ) AS migration_out_of_order
\\gset
\\if :migration_drift
    \\warn Migration checksum or name drift detected for :migration_name
    \\quit 3
\\endif
\\if :migration_out_of_order
    \\warn Refusing out-of-order migration :migration_name
    \\quit 4
\\endif
\\if :migration_applied
    \\echo Migration :migration_name was already applied
\\else
{migration.path.read_text()}

DO $assert_execution_table_registry$
BEGIN
    IF to_regprocedure(
        'public.apdl_assert_execution_table_registry()'
    ) IS NOT NULL THEN
        PERFORM public.apdl_assert_execution_table_registry();
    END IF;
END
$assert_execution_table_registry$;

INSERT INTO public.{LEDGER_TABLE} (version, name, checksum)
VALUES (:migration_version, :'migration_name', :'migration_checksum');
\\endif
COMMIT;
"""
    _psql(
        wrapper,
        fence,
        variables={
            "migration_version": str(migration.version),
            "migration_name": migration.name,
            "migration_checksum": migration.checksum,
        },
    )


def migrate(directory: Path) -> tuple[Migration, ...]:
    migrations = discover_migrations(directory)
    _wait_for_postgres()
    with _maintenance_fence() as fence:
        _ensure_ledger(fence)
        applied = _read_ledger(fence)
        _assert_fresh_database_for_empty_ledger(applied, fence)
        pending = plan_migrations(migrations, applied)
        for migration in pending:
            print(f"  Applying {migration.name}", flush=True)
            _apply_migration(migration, fence)
        if not pending:
            print("  PostgreSQL schema is already current", flush=True)
    return pending


def main() -> int:
    directory = Path(os.environ.get("POSTGRES_MIGRATIONS_DIR", "/migrations"))
    try:
        migrate(directory)
    except (
        MigrationError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"PostgreSQL migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
