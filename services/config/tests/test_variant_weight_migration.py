"""Contracts for migration 042's fail-closed variant repair."""

from pathlib import Path

from app import schema


MIGRATION_SQL = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "042_variant_weight_contract.sql"
).read_text()


def test_migration_pins_exact_safe_integer_and_count_bounds():
    assert "jsonb_array_length(variants_value) > 10" in MIGRATION_SQL
    assert "variant_weight > 9007199254740991" in MIGRATION_SQL
    assert "total_weight > 9007199254740991" in MIGRATION_SQL
    assert "RETURN total_weight > 0 AND default_observed" in MIGRATION_SQL
    assert "apdl_flag_variants_are_canonical" in MIGRATION_SQL
    assert "apdl_experiment_variants_are_canonical" in MIGRATION_SQL


def test_invalid_experiment_bundle_is_stopped_disabled_and_audited():
    repair = MIGRATION_SQL.index(
        "DO $repair_invalid_experiment_variant_bundles$"
    )
    flag_constraint = MIGRATION_SQL.index(
        "ADD CONSTRAINT flags_variants_canonical_check"
    )
    experiment_constraint = MIGRATION_SQL.index(
        "ADD CONSTRAINT experiments_variants_canonical_check"
    )

    assert repair < flag_constraint < experiment_constraint
    assert (
        "experiment.status IN ('scheduled', 'running')"
        in MIGRATION_SQL
    )
    assert "THEN 'stopped'" in MIGRATION_SQL
    assert "ELSE 'disabled'" in MIGRATION_SQL
    assert "'invalid_variant_configuration'" in MIGRATION_SQL
    assert "'system:migration:042'" in MIGRATION_SQL
    assert "version = experiment.version + 1" in MIGRATION_SQL
    assert "version = flag.version + 1" in MIGRATION_SQL
    assert "INSERT INTO flag_audit_log" in MIGRATION_SQL
    assert "INSERT INTO experiment_audit_log" in MIGRATION_SQL


def test_repair_uses_one_project_version_for_both_bundle_outbox_intents():
    bundle = MIGRATION_SQL[
        MIGRATION_SQL.index(
            "DO $repair_invalid_experiment_variant_bundles$"
        ):
        MIGRATION_SQL.index(
            "$repair_invalid_experiment_variant_bundles$;",
            MIGRATION_SQL.index(
                "DO $repair_invalid_experiment_variant_bundles$"
            ),
        )
    ]
    assert bundle.count("INSERT INTO config_project_versions") == 1
    assert "'flag_change'" in bundle
    assert "'experiment_change'" in bundle
    assert bundle.count("'project_version', next_project_version") == 2
    assert "'variants', repaired_flag.variants" in bundle


def test_invalid_standalone_flag_is_disabled_repaired_and_delivered():
    standalone = MIGRATION_SQL[
        MIGRATION_SQL.index(
            "DO $repair_invalid_standalone_flag_variants$"
        ):
        MIGRATION_SQL.index(
            "$repair_invalid_standalone_flag_variants$;",
            MIGRATION_SQL.index(
                "DO $repair_invalid_standalone_flag_variants$"
            ),
        )
    ]
    assert "ELSE 'disabled'" in standalone
    assert "enabled = false" in standalone
    assert "default_variant = 'control'" in standalone
    assert "'key', 'treatment', 'weight', 1" in standalone
    assert "version = flag.version + 1" in standalone
    assert "INSERT INTO flag_audit_log" in standalone
    assert "INSERT INTO config_project_versions" in standalone
    assert "'flag_change'" in standalone


def test_migration_fails_if_invalid_experiment_cannot_be_disabled():
    preflight = MIGRATION_SQL[
        MIGRATION_SQL.index("DO $require_repairable_invalid_experiments$"):
        MIGRATION_SQL.index("$require_repairable_invalid_experiments$;")
    ]
    assert "NOT EXISTS" in preflight
    assert "backing flag authority" in preflight


def test_config_startup_retains_both_variant_constraints_after_migration_042():
    assert schema.MIGRATION_VERSION >= 42
    assert {
        ("flags", "flags_variants_canonical_check"),
        ("experiments", "experiments_variants_canonical_check"),
    } <= schema.REQUIRED_CONSTRAINTS
    assert (
        "VALIDATE CONSTRAINT flags_variants_canonical_check"
        in MIGRATION_SQL
    )
    assert (
        "VALIDATE CONSTRAINT experiments_variants_canonical_check"
        in MIGRATION_SQL
    )
