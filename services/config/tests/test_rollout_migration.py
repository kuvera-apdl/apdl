"""Contract checks for canonical rollout migration 017."""

from pathlib import Path


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "017_config_rollout_contract.sql"
).read_text()


def test_migration_validates_rule_and_fallthrough_rollouts_without_null_bypass():
    assert "apdl_rollout_is_canonical" in MIGRATION_SQL
    assert "jsonb_typeof(value->'percentage') IS DISTINCT FROM 'number'" in MIGRATION_SQL
    assert "percentage < 0 OR percentage > 100" in MIGRATION_SQL
    assert "char_length(value->>'bucket_by') BETWEEN 1 AND 128" in MIGRATION_SQL
    assert "apdl_rules_rollouts_are_canonical" in MIGRATION_SQL
    assert "apdl_flag_rollouts_are_canonical" in MIGRATION_SQL
    assert "IS NOT TRUE" in MIGRATION_SQL


def test_migration_repairs_and_audits_before_installing_constraint():
    repair = MIGRATION_SQL.index("DO $repair_invalid_flag_rollouts$")
    constraint = MIGRATION_SQL.index("ADD CONSTRAINT flags_rollouts_canonical_check")

    assert repair < constraint
    assert "flag_invalid_config_repaired" in MIGRATION_SQL
    assert "system:migration:017" in MIGRATION_SQL
    assert "'migration'" in MIGRATION_SQL
    assert "before_snapshot" in MIGRATION_SQL
    assert "after_snapshot" in MIGRATION_SQL
    assert "version = flag.version + 1" in MIGRATION_SQL
    assert "INSERT INTO config_project_versions" in MIGRATION_SQL
    assert "INSERT INTO config_outbox" in MIGRATION_SQL


def test_migration_repairs_experiment_sources_and_stops_active_lifecycles():
    assert "apdl_experiment_rules_are_canonical" in MIGRATION_SQL
    assert "invalid_experiment.invalid_traffic" in MIGRATION_SQL
    assert "invalid_experiment.invalid_rules" in MIGRATION_SQL
    assert "experiment.status IN ('scheduled', 'running') THEN 'stopped'" in MIGRATION_SQL
    assert "experiment.status = 'scheduled' THEN NULL" in MIGRATION_SQL
    assert "experiments_traffic_percentage_check" in MIGRATION_SQL
    assert "experiments_targeting_rollouts_check" in MIGRATION_SQL
    assert "'experiment_change'" in MIGRATION_SQL
