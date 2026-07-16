"""Static release invariants for project-scoped execution authority."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
MIGRATION_014 = MIGRATIONS / "014_disable_self_registered_agents.sql"
MIGRATION_015 = MIGRATIONS / "015_custom_agent_contracts_and_retry_lineage.sql"
MIGRATION_028 = MIGRATIONS / "028_admin_execution_authority.sql"
MIGRATION_029 = MIGRATIONS / "029_admin_managed_credentials.sql"
MIGRATION_030 = MIGRATIONS / "030_admin_login_risk.sql"
MIGRATION_RUNNER = ROOT / "pipeline" / "postgres" / "migrate.py"
SQL_014 = MIGRATION_014.read_text(encoding="utf-8")
SQL_015 = MIGRATION_015.read_text(encoding="utf-8")
SQL_028 = MIGRATION_028.read_text(encoding="utf-8")
SQL_030 = MIGRATION_030.read_text(encoding="utf-8")
RUNNER_SOURCE = MIGRATION_RUNNER.read_text(encoding="utf-8")
EXECUTION_ROLES = ("agents:run", "agents:manage", "agents:approve")
EXECUTION_TABLES = {
    "agent_runs",
    "custom_agent_test_runs",
    "agent_approval_commands",
    "agent_approval_effects",
    "agent_mutation_quota_reservations",
    "llm_calls",
    "codegen_changesets",
}


def test_postgres_migration_sequence_includes_the_complete_execution_fence() -> None:
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))

    assert names[13] == MIGRATION_014.name
    assert names[14] == MIGRATION_015.name
    assert names[27] == MIGRATION_028.name
    assert names[28] == MIGRATION_029.name
    assert names[29] == MIGRATION_030.name
    assert [name[:3] for name in names] == [
        f"{version:03d}" for version in range(1, len(names) + 1)
    ]


def test_creator_provenance_cannot_be_erased_by_user_deletion() -> None:
    assert "DROP CONSTRAINT IF EXISTS admin_projects_created_by_fkey" in SQL_014
    assert (
        "FOREIGN KEY (created_by) REFERENCES admin_users(user_id) ON DELETE RESTRICT"
        in SQL_014
    )
    assert "ON DELETE SET NULL" not in SQL_014
    assert "CREATE OR REPLACE FUNCTION reject_admin_project_creator_change()" in SQL_014
    assert "IF NEW.created_by IS DISTINCT FROM OLD.created_by THEN" in SQL_014
    assert "ERRCODE = '23514'" in SQL_014
    assert "BEFORE UPDATE OF created_by ON admin_projects" in SQL_014


def test_original_agent_run_fence_and_reconciliation_remain_intact() -> None:
    assert "SELECT project.created_by" in SQL_014
    assert "FOR KEY SHARE" in SQL_014
    assert "IF NOT FOUND THEN" in SQL_014
    assert "ERRCODE = '23503'" in SQL_014
    assert "IF project_creator IS NOT NULL THEN" in SQL_014
    assert "ERRCODE = '42501'" in SQL_014
    assert "BEFORE INSERT OR UPDATE OF project_id ON agent_runs" in SQL_014
    assert "LEFT JOIN admin_projects AS project" in SQL_014
    assert "project.project_id IS NULL OR project.created_by IS NOT NULL" in SQL_014
    assert "run.status IN ('started', 'running', 'waiting_approval')" in SQL_014
    assert "run.phase = 'resuming'" in SQL_014
    assert "run.status IN ('approved', 'rejected')" in SQL_014

    proposal_update = SQL_014.index("UPDATE feature_proposals AS proposal")
    run_update = SQL_014.index("UPDATE agent_runs AS run")
    assert proposal_update < run_update
    assert "claim_run_id = NULL" in SQL_014[proposal_update:run_update]

    fenced_run = SQL_014[
        run_update : SQL_014.index("DELETE FROM admin_user_projects")
    ]
    assert "SET status = 'failed'" in fenced_run
    assert "phase = 'execution_disabled'" in fenced_run
    assert "lease_owner_id = NULL" in fenced_run
    assert "lease_expires_at = NULL" in fenced_run


def test_migration_015_execution_table_is_not_covered_by_a_number_bump_only() -> None:
    assert "CREATE TABLE custom_agent_test_runs" in SQL_015
    assert "project_id TEXT NOT NULL" in SQL_015
    assert (
        "apdl_register_execution_table('public.custom_agent_test_runs'::regclass)"
        in SQL_028
    )
    assert "UPDATE custom_agent_test_runs AS test_run" in SQL_028
    assert "WHERE test_run.status = 'running'" in SQL_028
    assert "Project execution authorization is unavailable" in SQL_028


def test_execution_authority_is_canonical_audited_and_immutable() -> None:
    assert "CREATE TABLE admin_project_execution_authorizations" in SQL_028
    assert "'operator_provisioned'" in SQL_028
    assert "'self_registered_override'" in SQL_028
    for column in ("actor TEXT NOT NULL", "reason TEXT NOT NULL", "authorized_at"):
        assert column in SQL_028
    assert "apdl_validate_execution_authorization_provenance" in SQL_028
    assert "project_creator IS NOT NULL" in SQL_028
    assert "project_creator IS NULL" in SQL_028
    assert "apdl_reject_execution_authorization_mutation" in SQL_028
    assert (
        "BEFORE UPDATE OR DELETE ON admin_project_execution_authorizations"
        in SQL_028
    )

    backfill = SQL_028[
        SQL_028.index("INSERT INTO admin_project_execution_authorizations") :
        SQL_028.index(
            "CREATE OR REPLACE FUNCTION apdl_reject_execution_authorization_mutation"
        )
    ]
    assert "WHERE project.created_by IS NULL" in backfill
    assert "WHERE project.created_by IS NOT NULL" not in backfill
    assert "WHEN (NEW.created_by IS NULL)" in SQL_028
    assert "DROP TRIGGER IF EXISTS agent_runs_operator_project_only" in SQL_028
    assert (
        "DROP FUNCTION IF EXISTS reject_unavailable_agent_run_project()"
        in SQL_028
    )


def test_execution_roles_require_authorization_in_both_authority_tables() -> None:
    assert "CREATE OR REPLACE FUNCTION apdl_enforce_execution_roles()" in SQL_028
    for role in EXECUTION_ROLES:
        assert role in SQL_028
    assert (
        "BEFORE INSERT OR UPDATE OF project_id, roles ON admin_user_projects"
        in SQL_028
    )
    assert (
        "BEFORE INSERT OR UPDATE OF project_id, roles ON auth_credentials"
        in SQL_028
    )
    assert SQL_028.count("PERFORM apdl_assert_execution_project_authorized(") >= 2


def test_all_current_execution_tables_use_the_reusable_registry_fence() -> None:
    assert "CREATE TABLE apdl_execution_table_registry" in SQL_028
    assert "CREATE OR REPLACE FUNCTION apdl_register_execution_table" in SQL_028
    assert "requires a non-null TEXT project_id" in SQL_028
    assert "CREATE TRIGGER apdl_execution_project_authorized" in SQL_028
    registered = set(
        re.findall(
            r"apdl_register_execution_table\(\s*"
            r"'public\.([a-z0-9_]+)'::regclass\s*\)",
            SQL_028,
        )
    )
    assert EXECUTION_TABLES <= registered

    assert "CREATE OR REPLACE FUNCTION apdl_assert_execution_table_registry()" in SQL_028
    assert "SELECT apdl_assert_execution_table_registry();" in SQL_028
    assert "public.apdl_assert_execution_table_registry()" in RUNNER_SOURCE
    runner_assertion = RUNNER_SOURCE.index(
        "public.apdl_assert_execution_table_registry()"
    )
    ledger_insert = RUNNER_SOURCE.index(
        "INSERT INTO public.{LEDGER_TABLE} (version, name, checksum)"
    )
    assert runner_assertion < ledger_insert


def test_active_codegen_work_is_terminalized_before_the_table_fence() -> None:
    reconciliation = SQL_028.index("UPDATE codegen_changesets AS changeset")
    registration = SQL_028.index(
        "apdl_register_execution_table('public.codegen_changesets'::regclass)"
    )
    assert reconciliation < registration
    assert "SET status = 'error'" in SQL_028[reconciliation:registration]
    assert (
        "changeset.status IN ('queued', 'cloning', 'editing', 'pushing', 'pr_open')"
        in SQL_028[reconciliation:registration]
    )


def test_login_risk_migration_replaces_deterministic_account_lockout() -> None:
    assert "DROP COLUMN IF EXISTS failed_login_attempts" in SQL_030
    assert "DROP COLUMN IF EXISTS locked_until" in SQL_030
    assert "CREATE TABLE admin_login_rate_buckets" in SQL_030
    assert "scope IN ('global', 'network', 'device')" in SQL_030
    assert "CREATE TABLE admin_login_source_risk" in SQL_030
    assert "CREATE TABLE admin_login_account_risk" in SQL_030
    assert "CREATE TABLE admin_security_notifications" in SQL_030
    assert "suspicious_login_activity" in SQL_030
