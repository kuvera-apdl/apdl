"""Static invariants for human-managed reveal-once credentials."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "pipeline" / "postgres" / "migrations"
SQL_001 = (MIGRATIONS / "001_auth_credentials.sql").read_text(encoding="utf-8")
SQL_029 = (MIGRATIONS / "029_admin_managed_credentials.sql").read_text(
    encoding="utf-8"
)


def test_management_role_is_human_only_and_backfilled_to_project_creators() -> None:
    assert "'credentials:manage'" in SQL_029
    assert "'credentials:manage'" not in SQL_001
    assert "ALTER TABLE admin_user_projects" in SQL_029
    assert "project.created_by = membership.user_id" in SQL_029
    assert "membership.roles || ARRAY['credentials:manage']" in SQL_029


def test_managed_credentials_are_hash_only_durable_and_human_bound() -> None:
    assert "CREATE TABLE admin_managed_credentials" in SQL_029
    assert "REFERENCES auth_credentials(credential_id, project_id)" in SQL_029
    assert "credential.expires_at IS NOT NULL" in SQL_029
    assert "credential.actor_user_id IS NOT NULL" in SQL_029
    assert "'credentials:manage' = ANY(membership_roles)" in SQL_029
    assert "credential.roles <@ membership_roles" in SQL_029
    assert "credential rotation must preserve kind and roles" in SQL_029
    assert "key_hash" not in SQL_029[
        SQL_029.index("CREATE TABLE admin_managed_credentials") :
        SQL_029.index("CREATE TABLE admin_credential_audit")
    ]


def test_credential_lifecycle_audit_and_metadata_are_immutable() -> None:
    assert "CREATE TABLE admin_credential_audit" in SQL_029
    for action in ("'create'", "'rotate'", "'revoke'"):
        assert action in SQL_029
    assert "admin_credential_audit_rotation_shape" in SQL_029
    assert "admin_managed_credentials_one_successor_idx" in SQL_029
    assert "apdl_reject_managed_credential_history_mutation" in SQL_029
    assert "BEFORE UPDATE OR DELETE ON admin_managed_credentials" in SQL_029
    assert "BEFORE TRUNCATE ON admin_managed_credentials" in SQL_029
    assert "BEFORE UPDATE OR DELETE ON admin_credential_audit" in SQL_029
    assert "BEFORE TRUNCATE ON admin_credential_audit" in SQL_029
