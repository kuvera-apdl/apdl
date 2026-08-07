"""Contracts for operator recovery and privacy-retention migration 044."""

from pathlib import Path

from app import schema

ROOT = Path(__file__).resolve().parents[3]
MIGRATION_SQL = (
    ROOT
    / "pipeline"
    / "postgres"
    / "migrations"
    / "044_operator_recovery_and_retention.sql"
).read_text()


def test_outbox_recovery_is_indexed_and_audited_without_payload_copy():
    assert "CREATE INDEX idx_config_outbox_metrics_pending" in MIGRATION_SQL
    assert "INCLUDE (attempts)" in MIGRATION_SQL
    assert "CREATE INDEX idx_config_outbox_quarantine_project_id" in MIGRATION_SQL
    assert "ON config_outbox (project_id, id DESC)" in MIGRATION_SQL
    quarantine_index = MIGRATION_SQL[
        MIGRATION_SQL.index("CREATE INDEX idx_config_outbox_quarantine_project_id") :
        MIGRATION_SQL.index("CREATE INDEX idx_experiment_audit_keyset")
    ]
    assert "WHERE quarantined_at IS NOT NULL" in quarantine_index
    assert "CREATE TABLE config_outbox_operator_log" in MIGRATION_SQL
    operator_log = MIGRATION_SQL[
        MIGRATION_SQL.index("CREATE TABLE config_outbox_operator_log") :
        MIGRATION_SQL.index("CREATE INDEX idx_config_outbox_operator_log_project")
    ]
    assert "payload_sha256" in operator_log
    assert "payload JSONB" not in operator_log
    assert "config_outbox_operator_log_no_update_delete" in MIGRATION_SQL


def test_experiment_audit_has_explicit_project_scoped_purge_authority():
    assert "CREATE TABLE experiment_audit_purge_log" in MIGRATION_SQL
    assert "SECURITY DEFINER" in MIGRATION_SQL
    assert "p_confirmation IS DISTINCT FROM 'PURGE EXPERIMENT AUDIT'" in MIGRATION_SQL
    assert "LOCK TABLE public.experiment_audit_log IN SHARE ROW EXCLUSIVE MODE" in (
        MIGRATION_SQL
    )
    assert "WHERE project_id = p_project_id" in MIGRATION_SQL
    assert "AND created_at < p_purge_before" in MIGRATION_SQL
    assert "session_user" in MIGRATION_SQL
    assert "p_actor" not in MIGRATION_SQL
    assert "DISABLE TRIGGER" not in MIGRATION_SQL
    assert "INSERT INTO public.experiment_audit_purge_log" in MIGRATION_SQL
    assert "GRANT SELECT, DELETE\n    ON experiment_audit_log" in MIGRATION_SQL
    assert ") OWNER TO apdl_audit_purge_definer" in MIGRATION_SQL
    assert ") TO apdl_audit_operator" in MIGRATION_SQL


def test_runtime_role_is_non_owner_and_cannot_inherit_other_roles():
    assert "required role apdl_runtime is missing" in MIGRATION_SQL
    assert "required role apdl_audit_operator is missing" in MIGRATION_SQL
    assert "required role apdl_audit_purge_definer is missing" in MIGRATION_SQL
    assert "apdl_runtime must be LOGIN NOINHERIT NOSUPERUSER" in MIGRATION_SQL
    assert "FROM pg_catalog.pg_auth_members" in MIGRATION_SQL
    assert "WHERE member = role_record.oid" in MIGRATION_SQL
    assert "WHERE roleid = role_record.oid" in MIGRATION_SQL
    assert "FROM pg_catalog.pg_shdepend" in MIGRATION_SQL
    assert "FROM pg_catalog.pg_database" in MIGRATION_SQL
    assert "FROM pg_catalog.pg_namespace" in MIGRATION_SQL
    assert "GRANT CONNECT ON DATABASE %I TO apdl_runtime" in MIGRATION_SQL
    assert "ON ALL TABLES IN SCHEMA public" in MIGRATION_SQL
    assert (
        "FROM PUBLIC, apdl_runtime;\nGRANT EXECUTE ON FUNCTION "
        "public.apdl_purge_experiment_audit"
    ) in MIGRATION_SQL


def test_purge_function_has_one_exact_definition_and_acl():
    assert "CREATE FUNCTION public.apdl_purge_experiment_audit(" in MIGRATION_SQL
    assert (
        "CREATE OR REPLACE FUNCTION public.apdl_purge_experiment_audit("
        not in MIGRATION_SQL
    )
    assert "SET search_path = pg_catalog" in MIGRATION_SQL
    assert "SET search_path = pg_catalog, public" not in MIGRATION_SQL
    assert "procedure.proname = 'apdl_purge_experiment_audit'" in MIGRATION_SQL
    assert "procedure.prosecdef" in MIGRATION_SQL
    assert "procedure.proconfig IS NOT DISTINCT FROM" in MIGRATION_SQL
    assert "pg_catalog.aclexplode(" in MIGRATION_SQL
    assert "count(*) = 2" in MIGRATION_SQL


def test_public_schema_and_operator_read_boundary_are_explicit():
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in MIGRATION_SQL
    assert "pg_catalog.has_schema_privilege(" in MIGRATION_SQL
    assert "GRANT USAGE ON SCHEMA public TO apdl_audit_operator" in MIGRATION_SQL
    assert (
        "ON experiment_audit_log, experiment_audit_purge_log"
        in MIGRATION_SQL
    )
    assert "apdl_audit_operator must have only read access" in MIGRATION_SQL


def test_config_schema_gate_includes_operator_recovery_contract():
    assert schema.MIGRATION_VERSION >= 44
    assert schema.MIGRATION_NAME.endswith(".sql")
    assert ("config_outbox_operator_log", "payload_sha256") in schema.REQUIRED_COLUMNS
    assert ("experiment_audit_purge_log", "deleted_rows") in schema.REQUIRED_COLUMNS
