"""Contracts for migration 043's explicit experiment bucketing identity."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "043_experiment_bucket_identity.sql"
).read_text()


def test_existing_experiments_preserve_the_only_prior_identity():
    assert "ADD COLUMN IF NOT EXISTS bucket_by TEXT" in MIGRATION_SQL
    assert "SET bucket_by = 'user_id'" in MIGRATION_SQL
    assert "WHERE bucket_by IS NULL" in MIGRATION_SQL
    assert "ALTER COLUMN bucket_by DROP DEFAULT" in MIGRATION_SQL
    assert "ALTER COLUMN bucket_by SET NOT NULL" in MIGRATION_SQL


def test_migration_installs_and_validates_exact_identity_constraint():
    assert "bucket_by IN ('anonymous_id', 'user_id')" in MIGRATION_SQL
    assert "ADD CONSTRAINT experiments_bucket_by_check" in MIGRATION_SQL
    assert "VALIDATE CONSTRAINT experiments_bucket_by_check" in MIGRATION_SQL


def test_bucket_identity_is_immutable_after_draft_in_postgres():
    assert "OLD.status <> 'draft'" in MIGRATION_SQL
    assert "NEW.bucket_by IS DISTINCT FROM OLD.bucket_by" in MIGRATION_SQL
    assert (
        "BEFORE UPDATE OF status, bucket_by, traffic_percentage"
        in MIGRATION_SQL
    )


def test_config_startup_requires_migration_043_column_and_constraint():
    assert schema.MIGRATION_VERSION == 43
    assert schema.MIGRATION_NAME == "043_experiment_bucket_identity.sql"
    assert ("experiments", "bucket_by") in schema.REQUIRED_COLUMNS
    assert (
        "experiments",
        "experiments_bucket_by_check",
    ) in schema.REQUIRED_CONSTRAINTS
