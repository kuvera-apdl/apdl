"""Repository authority is independent from tenant project credentials."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.connection import ConnectionCreate
from app.store.connections import (
    activate_operator_grant,
    delete_connection,
    get_connection,
    get_connection_for_changeset,
    revoke_repository_grant,
    upsert_connection,
)
from tests.fakes import FakePool


def test_connection_create_accepts_only_an_existing_grant_reference():
    payload = ConnectionCreate(project_id="demo", grant_id="ghg_verifiedgrant")
    assert payload.default_base_branch == "main"

    with pytest.raises(ValidationError):
        ConnectionCreate.model_validate(
            {
                "project_id": "demo",
                "grant_id": "ghg_verifiedgrant",
                "repository_full_name": "shared/other-tenant",
                "installation_id": 42,
            }
        )


@pytest.mark.asyncio
async def test_connection_cannot_reference_another_projects_grant():
    pool = FakePool()
    pool.add_connection("other", grant_id="ghg_otherrepository")

    with pytest.raises(ValueError, match="same-project"):
        await upsert_connection(
            pool,
            ConnectionCreate(
                project_id="demo",
                grant_id="ghg_otherrepository",
            ),
        )


@pytest.mark.asyncio
async def test_operator_activation_revokes_old_grant_and_preserves_tenant_policy():
    pool = FakePool()
    pool.add_connection(
        "demo",
        grant_id="ghg_originalrepository",
        tenant_policy={
            "schema_version": "tenant_codegen_connection_policy@1",
            "test_cmd": "make ci",
            "gates": {
                "max_files": 5,
                "max_lines": 300,
                "additional_protected_paths": ["infra/**"],
            },
            "runtime_acceptance": {
                "schema_version": "runtime_acceptance_request@1",
                "enabled": False,
            },
        },
    )

    connection = await activate_operator_grant(
        pool,
        project_id="demo",
        installation_id=99,
        repository_id=1234,
        repository_full_name="acme/verified",
        default_base_branch="develop",
        authorization_subject="operator@example.com",
    )

    assert pool.store["repository_grants"]["ghg_originalrepository"]["status"] == (
        "revoked"
    )
    assert connection.repository_id == 1234
    assert connection.repository_full_name == "acme/verified"
    assert connection.default_base_branch == "develop"
    assert connection.tenant_policy.test_cmd == "make ci"
    assert connection.target.installation_id == 99
    assert "installation_id" not in connection.model_dump(mode="json")
    assert "target" not in connection.model_dump(mode="json")
    assert pool.store["grant_notifications"] == [
        {
            "channel": "codegen_repository_grant_revoked",
            "grant_id": "ghg_originalrepository",
        }
    ]


@pytest.mark.asyncio
async def test_changeset_target_does_not_depend_on_current_connection():
    pool = FakePool()
    pool.add_connection(
        "demo",
        grant_id="ghg_originalrepository",
        installation_id=42,
        repository_id=123,
        repo="acme/widgets",
    )
    pool.add_changeset("cs_bound", "demo")

    assert await delete_connection(pool, "demo")
    target = await get_connection_for_changeset(pool, "cs_bound")

    assert target is not None
    assert target.grant_id == "ghg_originalrepository"
    assert target.repository_id == 123
    assert target.repository_full_name == "acme/widgets"
    assert target.target.installation_id == 42


@pytest.mark.asyncio
async def test_operator_revocation_immediately_hides_active_connection():
    pool = FakePool()
    pool.add_connection("demo", grant_id="ghg_verifiedgrant")

    assert await revoke_repository_grant(
        pool,
        project_id="demo",
        grant_id="ghg_verifiedgrant",
    )
    assert await get_connection(pool, "demo") is None
    assert pool.store["grant_notifications"] == [
        {
            "channel": "codegen_repository_grant_revoked",
            "grant_id": "ghg_verifiedgrant",
        }
    ]
    assert not await revoke_repository_grant(
        pool,
        project_id="demo",
        grant_id="ghg_verifiedgrant",
    )


def test_migration_quarantines_legacy_bindings_and_requires_snapshots():
    migration = (
        Path(__file__).parents[3]
        / "pipeline/postgres/migrations/009_codegen_repository_authority.sql"
    ).read_text()

    assert "RENAME TO codegen_connections_legacy_unverified" in migration
    assert "REFERENCES admin_projects (project_id)" in migration
    assert "codegen_connections_require_active_grant" in migration
    assert "github_repository_grants_prevent_delete" in migration
    assert "repository_target_quarantined BOOLEAN NOT NULL DEFAULT true" in migration
    assert "ALTER COLUMN repository_target_quarantined SET DEFAULT false" in migration
    assert "A changeset repository target is immutable after creation" in migration
    assert "INSERT INTO github_repository_grants" not in migration
