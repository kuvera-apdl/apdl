#!/usr/bin/env python3
"""Apply the immutable APDL PostgreSQL migration sequence."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")
LEDGER_TABLE = "apdl_schema_migrations"
ADVISORY_LOCK_ID = 4_158_044_082


class MigrationError(RuntimeError):
    """The on-disk sequence or persisted migration ledger is invalid."""


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


def _psql(
    sql: str,
    *,
    variables: dict[str, str] | None = None,
    capture: bool = False,
) -> str:
    command = ["psql", "-X", "-v", "ON_ERROR_STOP=1"]
    if capture:
        command.extend(("-A", "-t", "-F", "|"))
    for key, value in (variables or {}).items():
        command.extend(("-v", f"{key}={value}"))
    result = subprocess.run(
        command,
        input=sql,
        text=True,
        check=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
    )
    return result.stdout if capture else ""


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


def _ensure_ledger() -> None:
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
"""
    )


def _read_ledger() -> tuple[AppliedMigration, ...]:
    output = _psql(
        f"""
SELECT version, name, checksum
FROM public.{LEDGER_TABLE}
ORDER BY version;
""",
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


def _apply_migration(migration: Migration) -> None:
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
        variables={
            "migration_version": str(migration.version),
            "migration_name": migration.name,
            "migration_checksum": migration.checksum,
        },
    )


def migrate(directory: Path) -> tuple[Migration, ...]:
    migrations = discover_migrations(directory)
    _wait_for_postgres()
    _ensure_ledger()
    applied = _read_ledger()
    _assert_fresh_database_for_empty_ledger(applied)
    pending = plan_migrations(migrations, applied)
    for migration in pending:
        print(f"  Applying {migration.name}", flush=True)
        _apply_migration(migration)
    if not pending:
        print("  PostgreSQL schema is already current", flush=True)
    return pending


def main() -> int:
    directory = Path(os.environ.get("POSTGRES_MIGRATIONS_DIR", "/migrations"))
    try:
        migrate(directory)
    except (MigrationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"PostgreSQL migration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
