"""Contract checks for Config project-version migration 016."""

from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "016_config_project_versions.sql"
).read_text()


def test_migration_adds_strict_monotonic_project_version_table():
    assert "CREATE TABLE config_project_versions" in MIGRATION_SQL
    assert "project_id TEXT PRIMARY KEY" in MIGRATION_SQL
    assert "project_version BIGINT NOT NULL" in MIGRATION_SQL
    assert "CHECK (project_version >= 0)" in MIGRATION_SQL


def test_migration_stamps_existing_config_outbox_rows_in_order():
    assert "row_number() OVER" in MIGRATION_SQL
    assert "PARTITION BY project_id" in MIGRATION_SQL
    assert "ORDER BY id" in MIGRATION_SQL
    assert "outbox.payload - 'version'" in MIGRATION_SQL
    assert "config_outbox_project_version_check" in MIGRATION_SQL
    assert "payload ? 'project_version'" in MIGRATION_SQL
    assert "IS NOT DISTINCT FROM 'number'" in MIGRATION_SQL
    assert "COALESCE(" in MIGRATION_SQL
