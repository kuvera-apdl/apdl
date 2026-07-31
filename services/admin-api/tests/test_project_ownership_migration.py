"""Static invariants for transferable human project ownership."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
SQL_001 = (MIGRATIONS / "001_auth_credentials.sql").read_text(encoding="utf-8")
SQL_046 = (MIGRATIONS / "046_admin_project_ownership.sql").read_text(
    encoding="utf-8"
)


def test_members_manage_is_human_only_and_backfilled_to_creators() -> None:
    assert "'members:manage'" in SQL_046
    assert "'members:manage'" not in SQL_001
    assert "project.created_by = membership.user_id" in SQL_046
    assert "membership.roles || ARRAY['members:manage']" in SQL_046


def test_owner_is_separate_from_creator_and_requires_an_active_manager() -> None:
    assert "ADD COLUMN owner_user_id UUID" in SQL_046
    assert "SET owner_user_id = created_by" in SQL_046
    assert "project owner must be an active project member with members:manage" in SQL_046
    assert "admin_user_projects_protect_owner" in SQL_046
    assert "admin_users_protect_active_owner" in SQL_046
    assert "admin_projects_require_human_owner" in SQL_046


def test_ownership_audit_is_immutable() -> None:
    assert "CREATE TABLE admin_project_ownership_audit" in SQL_046
    assert "previous_owner_user_id" in SQL_046
    assert "new_owner_user_id" in SQL_046
    assert "admin_project_ownership_audit_no_update_delete" in SQL_046
    assert "admin_project_ownership_audit_no_truncate" in SQL_046
