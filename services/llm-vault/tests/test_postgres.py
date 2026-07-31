"""Live PostgreSQL lifecycle proof for the restricted vault role."""

from __future__ import annotations

import base64
import os
import uuid

import asyncpg
import pytest

from app.contracts import (
    AgentsProjectedModel,
    AgentsProjection,
    CodegenProjectedModel,
    CodegenProjection,
    CredentialAccessRequest,
)
from app.crypto import CredentialCipher
from app.rotation import (
    MAINTENANCE_GUARD_LOCK_ID,
    MAINTENANCE_INHIBITOR_LOCK_ID,
    rotate_active_credentials,
)
from app.store import ProjectLlmVaultStore, VaultNotFoundError


OWNER_URL = os.getenv("LLM_VAULT_TEST_OWNER_POSTGRES_URL", "").strip() or None
VAULT_URL = os.getenv("LLM_VAULT_TEST_POSTGRES_URL", "").strip() or None

if os.getenv("GITHUB_ACTIONS") == "true" and (OWNER_URL is None or VAULT_URL is None):
    raise RuntimeError(
        "LLM_VAULT_TEST_OWNER_POSTGRES_URL and LLM_VAULT_TEST_POSTGRES_URL "
        "are required in GitHub Actions"
    )

pytestmark = pytest.mark.skipif(
    OWNER_URL is None or VAULT_URL is None,
    reason="Live LLM Vault PostgreSQL URLs are not configured",
)


def _projection() -> CodegenProjection:
    return CodegenProjection(
        schema_version="codegen_llm_model_projection@1",
        catalog_version="codegen-provider-catalog@1",
        models=(
            CodegenProjectedModel(
                schema_version="codegen_provider_model@1",
                provider="openai",
                model_id="gpt-5.4-mini",
                display_name="GPT-5.4 Mini",
                supported_roles=("editor", "helper"),
                catalog_version="codegen-provider-catalog@1",
                context_window_tokens=400_000,
                supports_tool_calling=True,
                supports_structured_output=True,
                data_residency="global",
                allowed_data_classifications=(
                    "public",
                    "internal",
                    "confidential",
                ),
                input_cost_per_million_tokens_usd_micros=750_000,
                output_cost_per_million_tokens_usd_micros=4_500_000,
                pricing_status="catalog_reviewed",
            ),
        ),
    )


def _agents_projection() -> AgentsProjection:
    return AgentsProjection(
        schema_version="agents_llm_model_projection@1",
        catalog_version="llm-provider-catalog@2",
        models=(
            AgentsProjectedModel(
                schema_version="llm_provider_model@1",
                provider="openai",
                model_id="gpt-5.4-mini",
                display_name="GPT-5.4 Mini",
                supported_tiers=("fast", "reasoning"),
                catalog_version="llm-provider-catalog@2",
                data_residency="global",
                allowed_data_classifications=(
                    "public",
                    "internal",
                    "confidential",
                ),
                pricing_status="catalog_reviewed",
            ),
        ),
    )


async def _create_owner_project(project_id: str) -> uuid.UUID:
    assert OWNER_URL is not None
    actor_user_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_URL)
    try:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO admin_users (user_id, email, password_hash, active)
                VALUES ($1, $2, '$argon2id$vault-live-fixture', TRUE)
                """,
                actor_user_id,
                f"{actor_user_id.hex}@vault.test",
            )
            await conn.execute(
                "INSERT INTO admin_projects (project_id) VALUES ($1)",
                project_id,
            )
            await conn.execute(
                """
                INSERT INTO admin_user_projects (user_id, project_id, roles)
                VALUES (
                    $1, $2,
                    ARRAY[
                        'agents:read', 'agents:manage', 'credentials:manage',
                        'members:manage'
                    ]::TEXT[]
                )
                """,
                actor_user_id,
                project_id,
            )
            await conn.execute(
                """
                UPDATE admin_projects SET owner_user_id = $2
                WHERE project_id = $1
                """,
                project_id,
                actor_user_id,
            )
    finally:
        await conn.close()
    return actor_user_id


@pytest.mark.asyncio
async def test_create_issue_and_revoke_with_vault_only_secret_access() -> None:
    assert VAULT_URL is not None
    project_id = f"vault{uuid.uuid4().hex}"
    actor_user_id = await _create_owner_project(project_id)
    key = bytes(range(32))
    cipher = CredentialCipher.from_base64(base64.b64encode(key).decode("ascii"))
    pool = await asyncpg.create_pool(VAULT_URL, min_size=1, max_size=3)
    store = ProjectLlmVaultStore(pool, cipher)
    provider_secret = f"provider-secret-{uuid.uuid4().hex}"
    try:
        created = await store.create(
            project_id=project_id,
            provider="openai",
            label="Primary",
            api_key=provider_secret,
            consumers=("agents", "codegen"),
            model_ids=("gpt-5.4-mini",),
            projections={
                "agents": _agents_projection(),
                "codegen": _projection(),
            },
            actor_user_id=actor_user_id,
        )
        assert created.consumers == ("agents", "codegen")
        assert created.model_count == 1

        with pytest.raises(asyncpg.CheckViolationError):
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        DELETE FROM llm_vault_connection_consumers
                        WHERE connection_id = $1
                        """,
                        created.connection_id,
                    )

        async with pool.acquire() as conn:
            credential = await conn.fetchrow(
                """
                SELECT credential_id, credential_version
                FROM llm_vault_provider_credentials
                WHERE connection_id = $1 AND state = 'active'
                """,
                created.connection_id,
            )
        request = CredentialAccessRequest(
            schema_version="llm_credential_access_request@1",
            project_id=project_id,
            provider="openai",
            consumer="codegen",
            execution_id="attempt-live-1",
            purpose="codegen.edit",
            expected_credential_id=credential["credential_id"],
            expected_credential_version=credential["credential_version"],
        )
        issued = await store.issue_access(request)
        assert issued.api_key == provider_secret
        assert provider_secret not in repr(issued)

        new_key = bytes(reversed(range(32)))
        new_cipher = CredentialCipher.from_base64(
            base64.b64encode(new_key).decode("ascii")
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_lock($1)",
                MAINTENANCE_INHIBITOR_LOCK_ID,
            )
            await conn.execute(
                "SELECT pg_advisory_lock($1)",
                MAINTENANCE_GUARD_LOCK_ID,
            )
            try:
                async with conn.transaction():
                    rotated_count, rotation_audit_ids = (
                        await rotate_active_credentials(
                            conn,
                            old_cipher=cipher,
                            new_cipher=new_cipher,
                            operator="test:vault-key-rotation",
                        )
                    )
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock($1)",
                    MAINTENANCE_GUARD_LOCK_ID,
                )
                await conn.execute(
                    "SELECT pg_advisory_unlock($1)",
                    MAINTENANCE_INHIBITOR_LOCK_ID,
                )
        assert rotated_count == 1
        assert len(rotation_audit_ids) == 1
        store = ProjectLlmVaultStore(pool, new_cipher)
        issued_after_rotation = await store.issue_access(request)
        assert issued_after_rotation.api_key == provider_secret
        assert provider_secret not in repr(issued_after_rotation)

        authority = await store.load_refresh_authority(
            connection_id=created.connection_id,
            project_id=project_id,
            expected_version=created.version,
            actor_user_id=actor_user_id,
        )
        assert authority.api_key == provider_secret
        assert provider_secret not in repr(authority)
        revoked = await store.revoke(
            authority=authority,
            reason="Live lifecycle test complete",
            actor_user_id=actor_user_id,
        )
        assert revoked.state == "revoked"
        with pytest.raises(VaultNotFoundError):
            await store.issue_access(request)

        async with pool.acquire() as conn:
            secret_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM llm_vault_provider_secrets
                WHERE credential_id = $1
                """,
                credential["credential_id"],
            )
            actions = await conn.fetch(
                """
                SELECT action FROM llm_vault_audit
                WHERE connection_id = $1 ORDER BY created_at, audit_id
                """,
                created.connection_id,
            )
            access_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM llm_vault_access_audit
                WHERE connection_id = $1 AND execution_id = 'attempt-live-1'
                """,
                created.connection_id,
            )
            agents_projection_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM llm_project_provider_connections
                WHERE project_id = $1 AND provider = 'openai'
                  AND state = 'revoked'
                """,
                project_id,
            )
            codegen_projection_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM codegen_project_provider_connections
                WHERE project_id = $1 AND provider = 'openai'
                  AND state = 'revoked'
                """,
                project_id,
            )
            rotation_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM llm_vault_key_rotation_audit
                WHERE connection_id = $1
                  AND operator = 'test:vault-key-rotation'
                """,
                created.connection_id,
            )
        assert secret_count == 0
        assert [row["action"] for row in actions] == ["create", "revoke"]
        assert access_count == 2
        assert agents_projection_count == 1
        assert codegen_projection_count == 1
        assert rotation_count == 1
    finally:
        await pool.close()
