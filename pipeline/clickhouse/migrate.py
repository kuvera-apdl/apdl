#!/usr/bin/env python3
"""Apply the immutable APDL ClickHouse migration sequence."""

from __future__ import annotations

import fcntl
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
BACKFILL_NAME = re.compile(r"^[0-9]{3}_[a-z0-9_]+\.sql$")
CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
LEDGER_TABLE = "apdl_schema_migrations"
BACKFILL_LEDGER_TABLE = "apdl_schema_backfills"
MAINTENANCE_GATE_TABLE = "apdl_maintenance_gate"
MAINTENANCE_GATE_AUTHORITY = "runtime-writes"
RUNTIME_QUERY_ID_PREFIX = "apdl-runtime-"
MAINTENANCE_OWNER_TABLE = "apdl_active_maintenance"
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
MAINTENANCE_CLIENT_HANDSHAKE_SECONDS = 5
PROTOTYPE_OBJECTS = frozenset({
    "events_v2",
    "events_dlq_v2",
    "decisions_v2",
    "feeds_v2",
    "flag_evaluations_v",
    "experiment_exposures_v",
    "agent_actions_v",
    "personalizations_v",
})


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
    sql: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


@dataclass(frozen=True)
class Backfill:
    name: str
    checksum: str
    sql: str


@dataclass(frozen=True)
class MaintenanceGateState:
    row_count: int
    generation_count: int
    generation: int
    writes_blocked: int


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
        on_started: Callable[[], None] | None = None,
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
            on_started=on_started,
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
    on_started: Callable[[], None] | None = None,
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
            on_started=on_started,
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
    on_started: Callable[[], None] | None = None,
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
        if on_started is not None:
            on_started()
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


def _sql_without_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", without_blocks)


def _validate_engine_authority(name: str, sql: str) -> None:
    lowered = sql.lower()
    if (
        "not clickhouse" in lowered
        or re.search(r"target:\s*postgresql", lowered)
        or re.search(r"psql\s+\$postgres_url", lowered)
        or "create extension if not exists vector" in lowered
    ):
        raise MigrationError(f"Misplaced PostgreSQL migration: {name}")

    prototype_pattern = "|".join(
        re.escape(object_name) for object_name in sorted(PROTOTYPE_OBJECTS)
    )
    for statement in _sql_without_comments(sql).split(";"):
        normalized = " ".join(statement.split()).lower()
        if not normalized or not re.search(
            rf"(?<![a-z0-9_])(?:{prototype_pattern})(?![a-z0-9_])",
            normalized,
        ):
            continue
        allowed_drop = re.fullmatch(
            rf"drop (?:table|view) if exists `?(?:{prototype_pattern})`?",
            normalized,
        )
        if allowed_drop is None:
            raise MigrationError(
                f"Unsupported prototype v2 schema operation in migration: {name}"
            )


def discover_migrations(directory: Path) -> tuple[Migration, ...]:
    """Return one contiguous sequence with checksums bound to exact SQL bytes."""
    if not directory.is_dir():
        raise MigrationError(f"ClickHouse migrations directory not found: {directory}")

    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise MigrationError(f"No ClickHouse migrations found in: {directory}")

    migrations: list[Migration] = []
    for expected_version, path in enumerate(paths, start=1):
        match = MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"Invalid ClickHouse migration name: {path.name}")
        version = int(match.group("version"))
        if version != expected_version:
            raise MigrationError(
                "ClickHouse migrations must be contiguous from 001; "
                f"expected {expected_version:03d}, found {path.name}"
            )

        payload = path.read_bytes()
        try:
            sql = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(
                f"ClickHouse migration is not UTF-8: {path.name}"
            ) from exc
        _validate_engine_authority(path.name, sql)
        migrations.append(
            Migration(
                version=version,
                name=path.name,
                checksum=hashlib.sha256(payload).hexdigest(),
                sql=sql,
            )
        )
    return tuple(migrations)


def plan_migrations(
    migrations: Iterable[Migration],
    applied: Iterable[AppliedMigration],
) -> tuple[Migration, ...]:
    """Validate that the durable ledger is an exact prefix and return pending SQL."""
    migration_sequence = tuple(migrations)
    applied_sequence = tuple(sorted(applied, key=lambda item: item.version))

    if len({item.version for item in applied_sequence}) != len(applied_sequence):
        raise MigrationError("ClickHouse migration ledger contains duplicate versions")
    if len({item.name for item in applied_sequence}) != len(applied_sequence):
        raise MigrationError("ClickHouse migration ledger contains duplicate names")
    if len(applied_sequence) > len(migration_sequence):
        raise MigrationError(
            "ClickHouse migration ledger references files absent from this release"
        )

    for index, applied_item in enumerate(applied_sequence):
        expected = migration_sequence[index]
        if applied_item.version != expected.version:
            raise MigrationError(
                "ClickHouse migration ledger is not an ordered prefix: "
                f"expected version {expected.version:03d}, "
                f"found {applied_item.version:03d}"
            )
        if applied_item.name != expected.name:
            raise MigrationError(
                f"ClickHouse migration {expected.version:03d} name drift: "
                f"ledger={applied_item.name}, file={expected.name}"
            )
        if applied_item.checksum != expected.checksum:
            raise MigrationError(
                f"ClickHouse migration {expected.name} checksum drift: "
                f"ledger={applied_item.checksum}, file={expected.checksum}"
            )

    return migration_sequence[len(applied_sequence) :]


def discover_backfills(directory: Path) -> tuple[Backfill, ...]:
    """Snapshot canonical backfills and bind execution to their exact bytes."""
    if not directory.is_dir():
        raise MigrationError(f"ClickHouse backfills directory not found: {directory}")
    backfills: list[Backfill] = []
    for path in sorted(directory.glob("*.sql")):
        if BACKFILL_NAME.fullmatch(path.name) is None:
            raise MigrationError(f"Invalid ClickHouse backfill name: {path.name}")
        payload = path.read_bytes()
        try:
            sql = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(
                f"ClickHouse backfill is not UTF-8: {path.name}"
            ) from exc
        backfills.append(
            Backfill(
                name=path.name,
                checksum=hashlib.sha256(payload).hexdigest(),
                sql=sql,
            )
        )
    return tuple(backfills)


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


def _maintenance_psql_command() -> list[str]:
    container_id = os.environ.get("POSTGRES_CONTAINER_ID", "")
    if not container_id:
        return ["psql"]
    return [
        "docker",
        "exec",
        "-i",
        container_id,
        "psql",
        "-U",
        os.environ.get("POSTGRES_USER", "apdl"),
        "-d",
        os.environ.get("POSTGRES_DB", "apdl"),
    ]


@contextmanager
def _maintenance_fence() -> Iterator[MaintenanceFence]:
    """Hold redundant exclusive barriers for the complete migration interval."""
    timeout_seconds = _maintenance_timeout_seconds()
    owners: list[tuple[subprocess.Popen[str], int]] = []

    def acquire_owner(lock_id: int) -> subprocess.Popen[str]:
        command = [
            *_maintenance_psql_command(),
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


class ClickHouseClient:
    """Minimal clickhouse-client adapter executed inside the Compose container."""

    def __init__(
        self,
        *,
        container_id: str,
        user: str,
        password: str,
        database: str,
        run_token: str | None = None,
    ) -> None:
        self.container_id = container_id
        self.user = user
        self.password = password
        self.database = database
        self.run_token = run_token or uuid.uuid4().hex
        if re.fullmatch(r"[0-9a-f]{32}", self.run_token) is None:
            raise MigrationError("ClickHouse maintenance run token is invalid")

    def execute(
        self,
        sql: str,
        *,
        fence: MaintenanceFence,
        capture: bool = False,
        multiquery: bool = False,
    ) -> str:
        operation_token = uuid.uuid4().hex
        query_id = f"apdl-maintenance-{self.run_token}-{operation_token}"
        pidfile = f"/tmp/apdl-maintenance-{operation_token}.pid"
        client_pid: list[int | None] = [None]
        client_command = [
            "clickhouse-client",
            "--user",
            self.user,
            "--password",
            self.password,
            "--database",
            self.database,
            "--query_id",
            query_id,
        ]
        if multiquery:
            client_command.append("--multiquery")
        if capture:
            client_command.extend(("--format", "TSVRaw"))
        command = [
            "docker",
            "exec",
            "-i",
            self.container_id,
            "sh",
            "-c",
            'umask 077; printf "%s\\n" "$$" > "$1"; shift; exec "$@"',
            "apdl-maintenance-wrapper",
            pidfile,
            *client_command,
        ]

        def read_client_pid() -> int | None:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "sh",
                    "-c",
                    'test -r "$1" && cat "$1"',
                    "apdl-maintenance-pid",
                    pidfile,
                ],
                check=False,
                timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            value = result.stdout.strip()
            if result.returncode != 0 or not value.isdecimal():
                return None
            pid = int(value)
            return pid if pid > 1 else None

        def container_client_is_live(pid: int) -> bool:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "sh",
                    "-c",
                    'if [ ! -e "/proc/$1" ]; then '
                    'printf "__APDL_PROCESS_ABSENT__"; exit 0; fi; '
                    'test -r "/proc/$1/cmdline" || exit 2; '
                    'tr "\\000" " " < "/proc/$1/cmdline"',
                    "apdl-maintenance-probe",
                    str(pid),
                ],
                check=False,
                timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if result.returncode != 0:
                raise MigrationError(
                    "Could not prove ClickHouse container client process state"
                )
            command_line = result.stdout.strip()
            if command_line == "__APDL_PROCESS_ABSENT__":
                return False
            if not command_line:
                raise MigrationError(
                    "ClickHouse container client process state was ambiguous"
                )
            return query_id in command_line

        def await_client_handshake() -> None:
            deadline = time.monotonic() + MAINTENANCE_CLIENT_HANDSHAKE_SECONDS
            while time.monotonic() < deadline:
                fence.assert_held()
                pid = read_client_pid()
                if pid is not None and container_client_is_live(pid):
                    client_pid[0] = pid
                    return
                time.sleep(0.05)
            raise MigrationError(
                "ClickHouse migration client did not complete its PID handshake"
            )

        def signal_container_client(pid: int, signal_name: str) -> None:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "sh",
                    "-c",
                    'kill -"$1" "$2"',
                    "apdl-maintenance-kill",
                    signal_name,
                    str(pid),
                ],
                check=False,
                timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        def terminate_container_client() -> None:
            pid = client_pid[0] or read_client_pid()
            if pid is None or not container_client_is_live(pid):
                return
            signal_container_client(pid, "TERM")
            deadline = time.monotonic() + MAINTENANCE_OPERATION_TERMINATION_SECONDS
            while True:
                if not container_client_is_live(pid):
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            signal_container_client(pid, "KILL")
            deadline = time.monotonic() + MAINTENANCE_OPERATION_TERMINATION_SECONDS
            while True:
                if not container_client_is_live(pid):
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            raise MigrationError(
                "ClickHouse container-side migration client did not terminate"
            )

        def remove_pidfile() -> None:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "rm",
                    "-f",
                    pidfile,
                ],
                check=False,
                timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        def cancel_query() -> None:
            terminate_container_client()
            subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "clickhouse-client",
                    "--user",
                    self.user,
                    "--password",
                    self.password,
                    "--database",
                    self.database,
                    "--query",
                    f"KILL QUERY WHERE query_id = '{query_id}' SYNC",
                ],
                check=True,
                timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            verification = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    self.container_id,
                    "clickhouse-client",
                    "--user",
                    self.user,
                    "--password",
                    self.password,
                    "--database",
                    self.database,
                    "--query",
                    "SELECT count() FROM system.processes "
                    f"WHERE query_id = '{query_id}'",
                ],
                check=True,
                timeout=MAINTENANCE_OPERATION_TERMINATION_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if verification.stdout.strip() != "0":
                raise MigrationError(
                    "ClickHouse did not confirm migration query termination"
                )

        try:
            return fence.run_command(
                command,
                input_text=sql,
                capture=capture,
                description=f"running ClickHouse query {query_id}",
                cancel=cancel_query,
                on_started=await_client_handshake,
            )
        finally:
            remove_pidfile()


def _validate_maintenance_owner(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    columns = client.execute(
        f"""
SELECT name, type
FROM system.columns
WHERE database = currentDatabase() AND table = '{MAINTENANCE_OWNER_TABLE}'
ORDER BY position;
""",
        fence=fence,
        capture=True,
    ).strip()
    if columns != "run_token\tFixedString(32)":
        raise MigrationError(
            f"ClickHouse {MAINTENANCE_OWNER_TABLE} has a non-canonical schema"
        )
    metadata = client.execute(
        f"""
SELECT
    engine,
    if(empty(sorting_key), '__empty__', sorting_key),
    if(empty(primary_key), '__empty__', primary_key)
FROM system.tables
WHERE database = currentDatabase() AND name = '{MAINTENANCE_OWNER_TABLE}';
""",
        fence=fence,
        capture=True,
    ).strip()
    if metadata != "TinyLog\t__empty__\t__empty__":
        raise MigrationError(
            f"ClickHouse {MAINTENANCE_OWNER_TABLE} has a non-canonical engine"
        )
    owner = client.execute(
        f"""
SELECT count(), uniqExact(run_token), any(toString(run_token))
FROM {MAINTENANCE_OWNER_TABLE};
""",
        fence=fence,
        capture=True,
    ).strip()
    if owner != f"1\t1\t{client.run_token}":
        raise MigrationError("ClickHouse maintenance owner token is not canonical")


def _drop_maintenance_owner(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    client.execute(
        f"DROP TABLE {MAINTENANCE_OWNER_TABLE};",
        fence=fence,
    )
    remaining = client.execute(
        "SELECT count() FROM system.tables "
        "WHERE database = currentDatabase() "
        f"AND name = '{MAINTENANCE_OWNER_TABLE}';",
        fence=fence,
        capture=True,
    ).strip()
    if remaining != "0":
        raise MigrationError("ClickHouse maintenance owner removal was not durable")


@contextmanager
def _maintenance_owner(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> Iterator[None]:
    """Acquire one durable owner that survives process and PostgreSQL crashes."""
    created = False
    try:
        try:
            client.execute(
                f"""
CREATE TABLE {MAINTENANCE_OWNER_TABLE} (
    run_token FixedString(32)
) ENGINE = TinyLog
AS SELECT toFixedString('{client.run_token}', 32) AS run_token;
""",
                fence=fence,
                multiquery=True,
            )
        except subprocess.CalledProcessError as exc:
            raise MigrationError(
                f"Could not acquire ClickHouse maintenance ownership. If "
                f"{MAINTENANCE_OWNER_TABLE} exists, do not remove it until all "
                "apdl-maintenance queries and container clients for its recorded "
                "run token are proven absent; manual recovery is required."
            ) from exc
        created = True
        _validate_maintenance_owner(client, fence)
        yield
    finally:
        if created:
            try:
                _drop_maintenance_owner(client, fence)
            except BaseException:
                print(
                    "CRITICAL: ClickHouse maintenance ownership could not be "
                    "released with proof; the durable owner is retained and "
                    "manual recovery is required",
                    file=sys.stderr,
                    flush=True,
                )
                raise


def _ensure_maintenance_gate(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    """Create and strictly validate the durable ClickHouse writer authority."""
    client.execute(
        f"""
CREATE TABLE IF NOT EXISTS {MAINTENANCE_GATE_TABLE} (
    authority String,
    generation UInt64,
    writes_blocked UInt8
) ENGINE = ReplacingMergeTree(generation)
ORDER BY authority;
""",
        fence=fence,
        multiquery=True,
    )
    columns = client.execute(
        f"""
SELECT name, type, is_in_primary_key, is_in_sorting_key
FROM system.columns
WHERE database = currentDatabase() AND table = '{MAINTENANCE_GATE_TABLE}'
ORDER BY position;
""",
        fence=fence,
        capture=True,
    )
    expected_columns = (
        "authority\tString\t1\t1\n"
        "generation\tUInt64\t0\t0\n"
        "writes_blocked\tUInt8\t0\t0"
    )
    if columns.strip() != expected_columns:
        raise MigrationError(
            f"ClickHouse {MAINTENANCE_GATE_TABLE} has a non-canonical schema"
        )

    table_metadata = client.execute(
        f"""
SELECT engine, sorting_key, primary_key, engine_full
FROM system.tables
WHERE database = currentDatabase() AND name = '{MAINTENANCE_GATE_TABLE}';
""",
        fence=fence,
        capture=True,
    ).strip()
    fields = table_metadata.split("\t")
    if (
        len(fields) != 4
        or fields[0] != "ReplacingMergeTree"
        or fields[1] != "authority"
        or fields[2] != "authority"
        or re.fullmatch(
            r"ReplacingMergeTree\(generation\) ORDER BY authority"
            r"(?: SETTINGS .*)?",
            fields[3],
        )
        is None
    ):
        raise MigrationError(
            f"ClickHouse {MAINTENANCE_GATE_TABLE} has a non-canonical engine"
        )


def _read_maintenance_gate_state(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> MaintenanceGateState:
    output = client.execute(
        f"""
SELECT
    count(),
    uniqExact(generation),
    max(generation),
    argMax(writes_blocked, generation)
FROM {MAINTENANCE_GATE_TABLE}
WHERE authority = '{MAINTENANCE_GATE_AUTHORITY}';
""",
        fence=fence,
        capture=True,
    ).strip()
    fields = output.split("\t")
    if len(fields) != 4 or not all(field.isdecimal() for field in fields):
        raise MigrationError("ClickHouse maintenance gate returned invalid state")
    state = MaintenanceGateState(*(int(field) for field in fields))
    if state.row_count != state.generation_count:
        raise MigrationError("ClickHouse maintenance gate has duplicate generations")
    if state.writes_blocked not in (0, 1):
        raise MigrationError("ClickHouse maintenance gate has an invalid state value")
    if state.row_count == 0 and (state.generation != 0 or state.writes_blocked != 0):
        raise MigrationError("ClickHouse maintenance gate has invalid empty state")
    return state


def _append_maintenance_gate_state(
    client: ClickHouseClient,
    fence: MaintenanceFence,
    *,
    writes_blocked: bool,
) -> MaintenanceGateState:
    before = _read_maintenance_gate_state(client, fence)
    if before.generation >= (2**64 - 1):
        raise MigrationError("ClickHouse maintenance gate generation is exhausted")
    generation = before.generation + 1
    state_value = int(writes_blocked)
    client.execute(
        f"INSERT INTO {MAINTENANCE_GATE_TABLE} VALUES "
        f"('{MAINTENANCE_GATE_AUTHORITY}', {generation}, {state_value});",
        fence=fence,
    )
    after = _read_maintenance_gate_state(client, fence)
    if after.generation != generation or after.writes_blocked != state_value:
        raise MigrationError("ClickHouse maintenance gate update was not durable")
    return after


def _count_active_writer_queries(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> int:
    output = client.execute(
        "SELECT count() FROM system.processes "
        f"WHERE startsWith(query_id, '{RUNTIME_QUERY_ID_PREFIX}');",
        fence=fence,
        capture=True,
    ).strip()
    if not output.isdecimal():
        raise MigrationError("ClickHouse writer query drain returned an invalid count")
    return int(output)


def _drain_writer_queries(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    """Kill and prove absence of every writer query after the durable gate closes."""
    deadline = time.monotonic() + _maintenance_timeout_seconds()
    while True:
        fence.assert_held()
        client.execute(
            "KILL QUERY WHERE "
            f"startsWith(query_id, '{RUNTIME_QUERY_ID_PREFIX}') SYNC;",
            fence=fence,
        )
        if _count_active_writer_queries(client, fence) == 0:
            return
        if time.monotonic() >= deadline:
            raise MigrationError(
                "ClickHouse writer queries did not drain before the deadline"
            )
        time.sleep(MAINTENANCE_OPERATION_HEARTBEAT_SECONDS)


@contextmanager
def _maintenance_writer_gate(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> Iterator[None]:
    """Hold the server-enforced writer gate closed across every schema mutation."""
    _ensure_maintenance_gate(client, fence)
    _append_maintenance_gate_state(client, fence, writes_blocked=True)
    _drain_writer_queries(client, fence)
    try:
        yield
    except BaseException:
        print(
            "CRITICAL: ClickHouse migration failed with writer authority blocked; "
            "a successful rerun is required to reopen writes",
            file=sys.stderr,
            flush=True,
        )
        raise
    else:
        _append_maintenance_gate_state(client, fence, writes_blocked=False)


def _ensure_ledger(client: ClickHouseClient, fence: MaintenanceFence) -> None:
    client.execute(
        f"""
CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    version UInt16,
    name String,
    checksum FixedString(64),
    applied_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(applied_at)
ORDER BY (version, name, checksum);
""",
        fence=fence,
        multiquery=True,
    )


def _read_ledger(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> tuple[AppliedMigration, ...]:
    output = client.execute(
        f"""
SELECT version, name, toString(checksum)
FROM {LEDGER_TABLE} FINAL
ORDER BY version, name, checksum;
""",
        fence=fence,
        capture=True,
    )
    applied: list[AppliedMigration] = []
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise MigrationError(
                f"Could not parse ClickHouse migration ledger row: {line!r}"
            )
        try:
            version = int(fields[0])
        except ValueError as exc:
            raise MigrationError(
                f"Invalid ClickHouse migration ledger version: {fields[0]!r}"
            ) from exc
        name = fields[1]
        checksum = fields[2]
        if MIGRATION_NAME.fullmatch(name) is None or CHECKSUM.fullmatch(checksum) is None:
            raise MigrationError(
                f"Invalid ClickHouse migration ledger row: {line!r}"
            )
        applied.append(AppliedMigration(version, name, checksum))
    return tuple(applied)


def _record_migration(
    client: ClickHouseClient,
    migration: Migration,
    fence: MaintenanceFence,
) -> None:
    client.execute(
        f"""
INSERT INTO {LEDGER_TABLE} (version, name, checksum)
VALUES ({migration.version}, '{migration.name}', '{migration.checksum}');
""",
        fence=fence,
        multiquery=True,
    )


def _ensure_backfill_ledger(
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    client.execute(
        f"""
CREATE TABLE IF NOT EXISTS {BACKFILL_LEDGER_TABLE} (
    name String,
    checksum FixedString(64),
    completed_at DateTime64(3, 'UTC') DEFAULT now64(3)
) ENGINE = ReplacingMergeTree(completed_at)
ORDER BY (name, checksum);
""",
        fence=fence,
        multiquery=True,
    )


def _recorded_backfill_checksum(
    client: ClickHouseClient,
    name: str,
    fence: MaintenanceFence,
) -> str:
    return client.execute(
        f"""
SELECT multiIf(
    count() = 0,
    '',
    uniqExact(checksum) = 1,
    toString(any(checksum)),
    '__multiple_checksums__'
)
FROM {BACKFILL_LEDGER_TABLE} FINAL
WHERE name = '{name}';
""",
        fence=fence,
        capture=True,
    ).strip()


def apply_backfills(
    backfills: Iterable[Backfill],
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    """Apply immutable one-time data backfills under the maintenance fence."""
    _ensure_backfill_ledger(client, fence)
    for backfill in backfills:
        recorded_checksum = _recorded_backfill_checksum(client, backfill.name, fence)
        if recorded_checksum:
            if recorded_checksum != backfill.checksum:
                raise MigrationError(
                    f"ClickHouse backfill checksum drift: {backfill.name}"
                )
            print(f"  Already applied {backfill.name}", flush=True)
            continue
        print(f"  Backfilling {backfill.name}", flush=True)
        client.execute(backfill.sql, fence=fence, multiquery=True)
        client.execute(
            f"""
INSERT INTO {BACKFILL_LEDGER_TABLE} (name, checksum, completed_at)
VALUES ('{backfill.name}', '{backfill.checksum}', now64(3));
""",
            fence=fence,
            multiquery=True,
        )
        recorded_after = _recorded_backfill_checksum(client, backfill.name, fence)
        if recorded_after != backfill.checksum:
            raise MigrationError(
                f"ClickHouse backfill {backfill.name} was not durably recorded"
            )


def _verify_schema_convergence(
    migrations_directory: Path,
    backfills: Iterable[Backfill],
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> None:
    """Re-prove the complete migration and backfill ledgers before reopening."""
    migrations = discover_migrations(migrations_directory)
    pending = plan_migrations(migrations, _read_ledger(client, fence))
    if pending:
        raise MigrationError("ClickHouse migration ledger did not converge")
    for backfill in backfills:
        recorded_checksum = _recorded_backfill_checksum(client, backfill.name, fence)
        if recorded_checksum != backfill.checksum:
            raise MigrationError(
                f"ClickHouse backfill ledger did not converge: {backfill.name}"
            )


@contextmanager
def _migration_lock(container_id: str, database: str) -> Iterator[None]:
    identity = hashlib.sha256(f"{container_id}\0{database}".encode()).hexdigest()[:24]
    lock_path = Path(os.environ.get("TMPDIR", "/tmp")) / (
        f"apdl-clickhouse-migrations-{identity}.lock"
    )
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MigrationError(
                "Another ClickHouse migration runner is active for this database"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def migrate(
    directory: Path,
    client: ClickHouseClient,
    fence: MaintenanceFence,
) -> tuple[Migration, ...]:
    migrations = discover_migrations(directory)
    _ensure_ledger(client, fence)
    applied = _read_ledger(client, fence)
    pending = plan_migrations(migrations, applied)
    for migration in pending:
        print(f"  Applying {migration.name}", flush=True)
        client.execute(migration.sql, fence=fence, multiquery=True)
        _record_migration(client, migration, fence)
        applied_after = _read_ledger(client, fence)
        plan_migrations(migrations, applied_after)
        if len(applied_after) != migration.version:
            raise MigrationError(
                f"ClickHouse migration {migration.name} was not durably recorded"
            )
    if not pending:
        print("  ClickHouse schema is already current", flush=True)
    return pending


def main() -> int:
    directory = Path(
        os.environ.get(
            "CLICKHOUSE_MIGRATIONS_DIR",
            Path(__file__).resolve().parent / "migrations",
        )
    )
    backfills_directory = Path(
        os.environ.get(
            "CLICKHOUSE_BACKFILLS_DIR",
            Path(__file__).resolve().parent / "backfills",
        )
    )
    container_id = os.environ.get("CLICKHOUSE_CONTAINER_ID", "")
    if not container_id:
        print(
            "ClickHouse migration failed: CLICKHOUSE_CONTAINER_ID is required",
            file=sys.stderr,
        )
        return 1
    client = ClickHouseClient(
        container_id=container_id,
        user=os.environ.get("CLICKHOUSE_USER", "apdl"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "apdl_dev"),
        database=os.environ.get("CLICKHOUSE_DB", "apdl"),
    )
    try:
        backfills = discover_backfills(backfills_directory)
        with _migration_lock(client.container_id, client.database):
            with _maintenance_fence() as fence:
                bootstrap_client = ClickHouseClient(
                    container_id=container_id,
                    user=client.user,
                    password=client.password,
                    database="default",
                    run_token=client.run_token,
                )
                bootstrap_client.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{client.database}`;",
                    fence=fence,
                    multiquery=True,
                )
                with _maintenance_owner(client, fence):
                    with _maintenance_writer_gate(client, fence):
                        migrate(directory, client, fence)
                        apply_backfills(backfills, client, fence)
                        _verify_schema_convergence(
                            directory,
                            backfills,
                            client,
                            fence,
                        )
    except (
        MigrationError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"ClickHouse migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
