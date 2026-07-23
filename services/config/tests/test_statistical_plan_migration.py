"""Static contracts for the immutable experiment statistical-plan migration."""

from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "018_experiment_statistical_plan.sql"
).read_text()


def test_migration_adds_nullable_json_plan_without_backfilling_history():
    assert "ADD COLUMN IF NOT EXISTS statistical_plan JSONB" in MIGRATION_SQL
    assert "apdl_experiment_statistical_plan_is_canonical" in MIGRATION_SQL
    assert "experiments_active_statistical_plan_check" in MIGRATION_SQL
    assert "experiments_enforce_statistical_plan" in MIGRATION_SQL
    assert "statistical_plan IS DISTINCT FROM OLD.statistical_plan" in MIGRATION_SQL
    assert "NOT VALID" in MIGRATION_SQL
    assert "UPDATE experiments" not in MIGRATION_SQL
    assert "DEFAULT" not in MIGRATION_SQL


def test_migration_quarantines_legacy_significance_derived_ship_verdicts():
    assert "UPDATE experiment_verdicts" in MIGRATION_SQL
    assert "WHERE verdict = 'ship' AND consumed = FALSE" in MIGRATION_SQL


def test_config_schema_gate_requires_the_new_migration():
    from app import schema

    assert schema.MIGRATION_VERSION >= 18
    assert ("experiments", "statistical_plan") in schema.REQUIRED_COLUMNS
    assert ("experiments", "creation_idempotency_key") in schema.REQUIRED_COLUMNS
    assert (
        "experiments",
        "creation_idempotency_request_sha256",
    ) in schema.REQUIRED_COLUMNS
