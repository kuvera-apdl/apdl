"""Contracts for database-authoritative experiment enrollment immutability."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "031_experiment_enrollment_immutability.sql"
).read_text()


def test_migration_freezes_both_enrollment_fields_after_draft():
    assert "OLD.status <> 'draft'" in MIGRATION_SQL
    assert (
        "NEW.traffic_percentage IS DISTINCT FROM OLD.traffic_percentage"
        in MIGRATION_SQL
    )
    assert (
        "NEW.targeting_rules_json IS DISTINCT FROM OLD.targeting_rules_json"
        in MIGRATION_SQL
    )
    assert "BEFORE UPDATE OF status, traffic_percentage, targeting_rules_json" in (
        MIGRATION_SQL
    )
    assert "UPDATE experiments" not in MIGRATION_SQL


def test_config_schema_gate_requires_enrollment_immutability_migration():
    assert schema.MIGRATION_VERSION >= 31
