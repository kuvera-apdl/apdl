"""Contracts for the canonical experiment-enrollment migration."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "035_experiment_enrollment_contract.sql"
).read_text()


def test_schema_gate_requires_enrollment_contract_migration():
    assert schema.MIGRATION_VERSION >= 35
    assert (
        "experiments",
        "minimum_exposure_config_version",
    ) in schema.REQUIRED_COLUMNS


def test_migration_installs_eligibility_only_targeting_contract():
    final_validator = MIGRATION_SQL.split(
        "CREATE OR REPLACE FUNCTION public.apdl_experiment_rules_are_canonical",
    )[-1]

    assert "(rule - 'id' - 'name' - 'conditions') <> '{}'::JSONB" in final_validator
    assert "rule ? 'rollout'" not in final_validator
    assert "DROP CONSTRAINT IF EXISTS experiments_targeting_rollouts_check" in (
        MIGRATION_SQL
    )
    assert "ADD CONSTRAINT experiments_targeting_rules_check" in MIGRATION_SQL


def test_migration_repairs_backing_projection_and_versions_atomically():
    repair_start = MIGRATION_SQL.index("DO $repair_targeted_experiment_flags$")
    assert MIGRATION_SQL.index(
        "DROP CONSTRAINT IF EXISTS experiments_targeting_rollouts_check"
    ) < repair_start
    assert "SET rules = projected_rules" in MIGRATION_SQL
    assert "'percentage', 0.0" in MIGRATION_SQL
    assert "version = flag.version + 1" in MIGRATION_SQL
    assert "version = experiment.version + 1" in MIGRATION_SQL
    assert "INSERT INTO flag_audit_log" in MIGRATION_SQL
    assert "INSERT INTO experiment_audit_log" in MIGRATION_SQL
    assert "'flag_change'" in MIGRATION_SQL
    assert "'experiment_change'" in MIGRATION_SQL
    assert "config_project_versions.project_version + 1" in MIGRATION_SQL


def test_migration_preserves_only_provably_compatible_history():
    assert "history_is_compatible BOOLEAN" in MIGRATION_SQL
    assert "= legacy_experiment.traffic_percentage::NUMERIC" in MIGRATION_SQL
    assert "rule->'rollout'->>'bucket_by' = 'user_id'" in MIGRATION_SQL
    assert "WHEN history_is_compatible THEN 1" in MIGRATION_SQL
    assert "ELSE repaired_flag.version" in MIGRATION_SQL
    assert "SET minimum_exposure_config_version = 1" in MIGRATION_SQL


def test_untargeted_backfill_is_versioned_audited_and_delivered():
    backfill = MIGRATION_SQL.split(
        "DO $backfill_untargeted_experiment_versions$",
        maxsplit=1,
    )[1].split(
        "$backfill_untargeted_experiment_versions$;",
        maxsplit=1,
    )[0]

    assert "version = experiment.version + 1" in backfill
    assert "INSERT INTO experiment_audit_log" in backfill
    assert "config_project_versions.project_version + 1" in backfill
    assert "'experiment_change'" in backfill


def test_migration_restores_stricter_lifecycle_guards():
    assert "status = 'draft'" in MIGRATION_SQL
    assert "minimum_exposure_config_version IS NULL" in MIGRATION_SQL
    assert "status <> 'draft'" in MIGRATION_SQL
    assert "minimum_exposure_config_version >= 1" in MIGRATION_SQL
    assert "NEW.minimum_exposure_config_version IS DISTINCT FROM" in MIGRATION_SQL
    assert "CREATE TRIGGER experiments_enforce_enrollment_immutability" in (
        MIGRATION_SQL
    )
    assert "CREATE TRIGGER experiments_enforce_archive_lifecycle" in MIGRATION_SQL
