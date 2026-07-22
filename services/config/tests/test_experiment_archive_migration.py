"""Contracts for durable experiment archival and lifecycle evidence."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "032_experiment_archive_lifecycle.sql"
).read_text()


def test_migration_preserves_launched_rows_as_immutable_tombstones():
    assert "ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ" in MIGRATION_SQL
    assert "ADD COLUMN IF NOT EXISTS archived_by TEXT" in MIGRATION_SQL
    assert "IF TG_OP = 'DELETE'" in MIGRATION_SQL
    assert "IF OLD.status <> 'draft'" in MIGRATION_SQL
    assert "only draft experiments may be physically deleted" in MIGRATION_SQL
    assert "IF OLD.archived_at IS NOT NULL" in MIGRATION_SQL
    assert "archived experiments are immutable" in MIGRATION_SQL
    assert "experiment archive must preserve the launched contract" in MIGRATION_SQL
    assert "archiving an open experiment must stop it" in MIGRATION_SQL
    assert "archived running experiment requires a bounded actual end" in MIGRATION_SQL


def test_migration_retains_append_only_lifecycle_evidence():
    assert "CREATE TABLE experiment_audit_log" in MIGRATION_SQL
    assert "experiment_archived" in MIGRATION_SQL
    assert "experiment_deleted" in MIGRATION_SQL
    assert "experiment_audit_log_no_update_delete" in MIGRATION_SQL
    assert "experiment_audit_log_no_truncate" in MIGRATION_SQL


def test_config_schema_gate_requires_archive_lifecycle_migration():
    assert schema.MIGRATION_VERSION >= 32
    assert ("experiments", "archived_at") in schema.REQUIRED_COLUMNS
    assert ("experiments", "archived_by") in schema.REQUIRED_COLUMNS
    assert ("experiment_audit_log", "action") in schema.REQUIRED_COLUMNS
