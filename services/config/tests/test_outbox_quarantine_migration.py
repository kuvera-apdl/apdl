"""Contracts for terminal Config outbox quarantine evidence."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "033_config_outbox_quarantine.sql"
).read_text()


def test_migration_adds_strict_terminal_quarantine_evidence():
    assert "ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMPTZ" in MIGRATION_SQL
    assert "ADD COLUMN IF NOT EXISTS failure_class TEXT" in MIGRATION_SQL
    assert "ADD COLUMN IF NOT EXISTS failure_code TEXT" in MIGRATION_SQL
    assert "processed_at IS NULL OR quarantined_at IS NULL" in MIGRATION_SQL
    assert "failure_class IN ('permanent', 'attempts_exhausted')" in MIGRATION_SQL
    assert "last_error <> ''" in MIGRATION_SQL


def test_pending_index_excludes_terminal_quarantine():
    assert "DROP INDEX IF EXISTS idx_config_outbox_pending" in MIGRATION_SQL
    assert "processed_at IS NULL AND quarantined_at IS NULL" in MIGRATION_SQL
    assert "idx_config_outbox_quarantined" in MIGRATION_SQL


def test_config_schema_gate_requires_outbox_quarantine_migration():
    assert schema.MIGRATION_VERSION == 33
    assert schema.MIGRATION_NAME == "033_config_outbox_quarantine.sql"
    assert ("config_outbox", "quarantined_at") in schema.REQUIRED_COLUMNS
    assert ("config_outbox", "failure_class") in schema.REQUIRED_COLUMNS
    assert ("config_outbox", "failure_code") in schema.REQUIRED_COLUMNS
