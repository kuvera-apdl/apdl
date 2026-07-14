"""Behavioral tests for the production PostgreSQL migration planner."""

from __future__ import annotations

import importlib.util
import sys
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
