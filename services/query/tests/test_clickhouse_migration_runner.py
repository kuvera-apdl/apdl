"""Behavioral tests for the production ClickHouse migration planner."""

from __future__ import annotations

import importlib.util
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
