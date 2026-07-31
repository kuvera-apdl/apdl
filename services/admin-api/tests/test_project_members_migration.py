"""Static invariants for secure project invitations and membership history."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
SQL_047 = (MIGRATIONS / "047_admin_project_members.sql").read_text(
    encoding="utf-8"
)


def test_human_roles_and_invitation_roles_use_one_canonical_order() -> None:
    assert "CREATE OR REPLACE FUNCTION apdl_canonical_admin_roles" in SQL_047
    assert "SET roles = apdl_canonical_admin_roles(roles)" in SQL_047
    assert SQL_047.count("roles = apdl_canonical_admin_roles(roles)") >= 2


def test_invitations_are_hash_only_seven_day_single_use_records() -> None:
    invitation_table = SQL_047[
        SQL_047.index("CREATE TABLE admin_project_invitations") :
        SQL_047.index("CREATE UNIQUE INDEX admin_project_invitations_pending_email_idx")
    ]
    assert "token_hash CHAR(64)" in invitation_table
    assert "raw_token" not in invitation_table
    assert "invitation_url" not in invitation_table
    assert "expires_at = created_at + INTERVAL '7 days'" in invitation_table
    assert "accepted_at" in invitation_table
    assert "revoked_at" in invitation_table
    assert "admin_project_invitations_pending_email_idx" in SQL_047


def test_invitation_and_membership_history_is_immutable() -> None:
    assert "CREATE TABLE admin_project_membership_audit" in SQL_047
    for action in (
        "'invitation_create'",
        "'invitation_revoke'",
        "'invitation_accept'",
        "'roles_replace'",
        "'member_remove'",
    ):
        assert action in SQL_047
    assert "apdl_validate_project_invitation_update" in SQL_047
    assert "admin_project_invitations_no_delete" in SQL_047
    assert "admin_project_invitations_no_truncate" in SQL_047
    assert "admin_project_membership_audit_no_update_delete" in SQL_047
    assert "admin_project_membership_audit_no_truncate" in SQL_047
