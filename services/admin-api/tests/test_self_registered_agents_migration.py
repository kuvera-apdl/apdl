"""Static contracts for fencing Agents on self-registered projects."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
MIGRATION_PATH = MIGRATIONS / "014_disable_self_registered_agents.sql"
SQL = MIGRATION_PATH.read_text(encoding="utf-8")
EXECUTION_ROLES = ("agents:run", "agents:manage", "agents:approve")


def test_self_registered_agents_migration_is_contiguous() -> None:
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert MIGRATION_PATH.name in names
    assert [name[:3] for name in names] == [
        f"{version:03d}" for version in range(1, len(names) + 1)
    ]


def test_creator_provenance_cannot_be_erased_by_user_deletion() -> None:
    assert "DROP CONSTRAINT IF EXISTS admin_projects_created_by_fkey" in SQL
    assert "FOREIGN KEY (created_by) REFERENCES admin_users(user_id) ON DELETE RESTRICT" in SQL
    assert "ON DELETE SET NULL" not in SQL
    assert "CREATE OR REPLACE FUNCTION reject_admin_project_creator_change()" in SQL
    assert "IF NEW.created_by IS DISTINCT FROM OLD.created_by THEN" in SQL
    assert "ERRCODE = '23514'" in SQL
    assert "BEFORE UPDATE OF created_by ON admin_projects" in SQL


def test_database_trigger_rejects_missing_and_self_registered_run_projects() -> None:
    assert "SELECT project.created_by" in SQL
    assert "FOR KEY SHARE" in SQL
    assert "IF NOT FOUND THEN" in SQL
    assert "ERRCODE = '23503'" in SQL
    assert "IF project_creator IS NOT NULL THEN" in SQL
    assert "ERRCODE = '42501'" in SQL
    assert "BEFORE INSERT OR UPDATE OF project_id ON agent_runs" in SQL

    # A NULL creator is the explicit operator/demo allow case.
    function = SQL[
        SQL.index("CREATE OR REPLACE FUNCTION reject_unavailable_agent_run_project()") :
        SQL.index("DROP TRIGGER IF EXISTS agent_runs_operator_project_only")
    ]
    assert "IF project_creator IS NULL" not in function
    assert function.index("IF project_creator IS NOT NULL") < function.index("RETURN NEW")


def test_in_flight_disallowed_runs_are_fenced_and_claims_are_reopened_first() -> None:
    assert "LEFT JOIN admin_projects AS project" in SQL
    assert "project.project_id IS NULL OR project.created_by IS NOT NULL" in SQL
    assert "run.status IN ('started', 'running', 'waiting_approval')" in SQL
    assert "run.phase = 'resuming'" in SQL
    assert "run.status IN ('approved', 'rejected')" in SQL

    proposal_update = SQL.index("UPDATE feature_proposals AS proposal")
    run_update = SQL.index("UPDATE agent_runs AS run")
    assert proposal_update < run_update
    assert "SET status = 'approved'" in SQL[proposal_update:run_update]
    assert "claim_run_id = NULL" in SQL[proposal_update:run_update]
    assert "error = NULL" in SQL[proposal_update:run_update]

    fenced_run = SQL[run_update : SQL.index("DELETE FROM admin_user_projects")]
    assert "SET status = 'failed'" in fenced_run
    assert "phase = 'execution_disabled'" in fenced_run
    assert "lease_owner_id = NULL" in fenced_run
    assert "lease_expires_at = NULL" in fenced_run


def test_execution_roles_are_removed_only_from_self_registered_projects() -> None:
    cleanup = SQL[SQL.index("DELETE FROM admin_user_projects") :]
    for role in EXECUTION_ROLES:
        assert cleanup.count(f"'{role}'") == 6

    assert cleanup.count("project.created_by IS NOT NULL") == 4
    assert "project.created_by IS NULL" not in cleanup
    assert "DELETE FROM admin_user_projects" in cleanup
    assert "UPDATE admin_user_projects" in cleanup
    assert "DELETE FROM auth_credentials" in cleanup
    assert "UPDATE auth_credentials" in cleanup

    # Empty sanitized arrays are deleted before non-empty rows are updated, so
    # neither table's canonical non-empty roles constraint can be violated.
    membership_delete = cleanup.index("DELETE FROM admin_user_projects")
    membership_update = cleanup.index("UPDATE admin_user_projects")
    credential_delete = cleanup.index("DELETE FROM auth_credentials")
    credential_update = cleanup.index("UPDATE auth_credentials")
    assert membership_delete < membership_update < credential_delete < credential_update
    assert cleanup.count("cardinality(") == 2
    assert cleanup.count(") = 0;") == 2
