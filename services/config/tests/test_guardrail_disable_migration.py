"""Static release contract for fail-closed automatic guardrails."""

from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "013_disable_automatic_guardrails.sql"
).read_text()


def test_migration_disables_existing_and_future_automatic_guardrails():
    assert "ALTER COLUMN auto_disable SET DEFAULT false" in MIGRATION_SQL
    assert "SET auto_disable = false" in MIGRATION_SQL
    assert "WHERE auto_disable = true" in MIGRATION_SQL
