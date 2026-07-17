#!/usr/bin/env python3
"""Apply the immutable APDL ClickHouse migration sequence."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")
CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
LEDGER_TABLE = "apdl_schema_migrations"
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


class ClickHouseClient:
    """Minimal clickhouse-client adapter executed inside the Compose container."""

    def __init__(
        self,
        *,
        container_id: str,
        user: str,
        password: str,
        database: str,
    ) -> None:
        self.container_id = container_id
        self.user = user
        self.password = password
        self.database = database

    def execute(
        self,
        sql: str,
        *,
        capture: bool = False,
        multiquery: bool = False,
    ) -> str:
        command = [
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
        ]
        if multiquery:
            command.append("--multiquery")
        if capture:
            command.extend(("--format", "TSVRaw"))
        result = subprocess.run(
            command,
            input=sql,
            text=True,
            check=True,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        )
        return result.stdout if capture else ""


def _ensure_ledger(client: ClickHouseClient) -> None:
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
        multiquery=True,
    )


def _read_ledger(client: ClickHouseClient) -> tuple[AppliedMigration, ...]:
    output = client.execute(
        f"""
SELECT version, name, toString(checksum)
FROM {LEDGER_TABLE} FINAL
ORDER BY version, name, checksum;
""",
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


def _record_migration(client: ClickHouseClient, migration: Migration) -> None:
    client.execute(
        f"""
INSERT INTO {LEDGER_TABLE} (version, name, checksum)
VALUES ({migration.version}, '{migration.name}', '{migration.checksum}');
""",
        multiquery=True,
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


def migrate(directory: Path, client: ClickHouseClient) -> tuple[Migration, ...]:
    migrations = discover_migrations(directory)
    with _migration_lock(client.container_id, client.database):
        _ensure_ledger(client)
        applied = _read_ledger(client)
        pending = plan_migrations(migrations, applied)
        for migration in pending:
            print(f"  Applying {migration.name}", flush=True)
            client.execute(migration.sql, multiquery=True)
            _record_migration(client, migration)
            applied_after = _read_ledger(client)
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
        migrate(directory, client)
    except (MigrationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ClickHouse migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
