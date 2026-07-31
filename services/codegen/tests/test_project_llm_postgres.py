"""Live PostgreSQL checks for vault-backed Codegen LLM authority.

Set ``CODEGEN_TEST_POSTGRES_URL`` to a disposable database after applying the
canonical migration sequence. CI treats this suite as mandatory.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

from app.capabilities import _project_llm_assignments
from app.llm.provider_catalog import CATALOG_VERSION, catalog_model
from app.store.llm_routing import assign_project_models


POSTGRES_URL = os.getenv("CODEGEN_TEST_POSTGRES_URL", "").strip() or None

if os.getenv("GITHUB_ACTIONS") == "true" and POSTGRES_URL is None:
    raise RuntimeError(
        "CODEGEN_TEST_POSTGRES_URL is required in GitHub Actions; "
        "the Codegen project LLM PostgreSQL suite must not be skipped"
    )

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="CODEGEN_TEST_POSTGRES_URL is not configured",
)


def _project_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


async def _create_project(
    conn: asyncpg.Connection,
    project_id: str,
) -> uuid.UUID:
    actor_user_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO admin_users (user_id, email, password_hash, active)
        VALUES ($1, $2, '$argon2id$vault-codegen-fixture', TRUE)
        """,
        actor_user_id,
        f"{actor_user_id.hex}@codegen.test",
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
        "UPDATE admin_projects SET owner_user_id = $2 WHERE project_id = $1",
        project_id,
        actor_user_id,
    )
    return actor_user_id


async def _seed_codegen_projection(
    conn: asyncpg.Connection,
    project_id: str,
    actor_user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID]:
    connection_id = uuid.uuid4()
    credential_id = uuid.uuid4()
    model = catalog_model("openai", "gpt-5.4-mini")
    assert model is not None
    await conn.execute(
        """
        INSERT INTO llm_vault_connections (
            connection_id, project_id, provider, label, version,
            inventory_version, state, validated_at, created_by_actor_user_id
        ) VALUES ($1, $2, 'openai', 'Primary', 1, 1, 'active', NOW(), $3)
        """,
        connection_id,
        project_id,
        actor_user_id,
    )
    await conn.execute(
        """
        INSERT INTO llm_vault_provider_credentials (
            credential_id, connection_id, project_id, provider,
            credential_version, state, created_by_actor_user_id
        ) VALUES ($1, $2, $3, 'openai', 1, 'active', $4)
        """,
        credential_id,
        connection_id,
        project_id,
        actor_user_id,
    )
    await conn.execute(
        """
        INSERT INTO llm_vault_provider_secrets (
            credential_id, ciphertext, nonce, algorithm, schema_version,
            encryption_key_id
        ) VALUES (
            $1, $2, $3, 'AES-256-GCM', 'llm_vault_provider_secret@1',
            'sha256:00000000000000000000000000000000'
        )
        """,
        credential_id,
        b"test-ciphertext-tag",
        connection_id.bytes[:12],
    )
    await conn.execute(
        """
        INSERT INTO llm_vault_connection_consumers (
            connection_id, project_id, provider, consumer,
            granted_by_actor_user_id
        ) VALUES ($1, $2, 'openai', 'agents', $3)
        """,
        connection_id,
        project_id,
        actor_user_id,
    )
    await conn.execute(
        """
        INSERT INTO llm_vault_provider_models (
            connection_id, connection_version, inventory_version,
            model_id, discovered_at
        ) VALUES ($1, 1, 1, 'gpt-5.4-mini', NOW())
        """,
        connection_id,
    )
    await conn.execute(
        """
        INSERT INTO codegen_project_provider_connections (
            project_id, provider, version, inventory_version, state,
            credential_id, catalog_version, validated_at, validated_by_actor
        ) VALUES (
            $1, 'openai', 1, 1, 'active', $2, $3, NOW(), 'test:vault-projection'
        )
        """,
        project_id,
        credential_id,
        CATALOG_VERSION,
    )
    await conn.execute(
        """
        INSERT INTO codegen_project_provider_models (
            project_id, provider, connection_version, inventory_version,
            schema_version, model_id, display_name, supported_roles,
            catalog_version, context_window_tokens, supports_tool_calling,
            supports_structured_output, data_residency,
            allowed_data_classifications,
            input_cost_per_million_tokens_usd_micros,
            output_cost_per_million_tokens_usd_micros,
            pricing_status, discovered_at
        ) VALUES (
            $1, 'openai', 1, 1, $2, $3, $4, $5, $6, $7, $8, $9,
            $10, $11, $12, $13, $14, NOW()
        )
        """,
        project_id,
        model.schema_version,
        model.model_id,
        model.display_name,
        list(model.supported_roles),
        model.catalog_version,
        model.context_window_tokens,
        model.supports_tool_calling,
        model.supports_structured_output,
        model.data_residency,
        list(model.allowed_data_classifications),
        model.input_cost_per_million_tokens_usd_micros,
        model.output_cost_per_million_tokens_usd_micros,
        model.pricing_status,
    )
    return connection_id, credential_id


async def _delete_project_fixture(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    connection_id: uuid.UUID,
) -> None:
    """Remove one synthetic authority graph from the disposable live database."""
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM codegen_project_model_assignments WHERE project_id = $1",
            project_id,
        )
        await conn.execute(
            "DELETE FROM codegen_project_provider_models WHERE project_id = $1",
            project_id,
        )
        await conn.execute(
            "DELETE FROM codegen_project_provider_connections WHERE project_id = $1",
            project_id,
        )
        await conn.execute(
            "DELETE FROM llm_vault_provider_models WHERE connection_id = $1",
            connection_id,
        )
        await conn.execute(
            "DELETE FROM llm_vault_connection_consumers WHERE connection_id = $1",
            connection_id,
        )
        await conn.execute(
            """
            DELETE FROM llm_vault_provider_secrets
            WHERE credential_id IN (
                SELECT credential_id FROM llm_vault_provider_credentials
                WHERE connection_id = $1
            )
            """,
            connection_id,
        )
        await conn.execute(
            "DELETE FROM llm_vault_provider_credentials WHERE connection_id = $1",
            connection_id,
        )
        await conn.execute(
            "DELETE FROM llm_vault_connections WHERE connection_id = $1",
            connection_id,
        )


@pytest.mark.asyncio
async def test_migration_removes_legacy_secrets_and_separates_privileges() -> None:
    assert POSTGRES_URL is not None
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        row = await conn.fetchrow(
            """
            SELECT
                to_regclass('public.llm_vault_provider_secrets')::TEXT
                    AS vault_secrets,
                to_regclass('public.codegen_project_provider_credentials')::TEXT
                    AS legacy_codegen,
                to_regclass('public.llm_project_provider_credentials')::TEXT
                    AS legacy_agents,
                has_table_privilege(
                    'apdl_runtime', 'public.llm_vault_provider_secrets', 'SELECT'
                ) AS runtime_can_read_secrets,
                has_table_privilege(
                    'apdl_llm_vault', 'public.llm_vault_provider_secrets', 'SELECT'
                ) AS vault_can_read_secrets
            """
        )
        assert dict(row) == {
            "vault_secrets": "llm_vault_provider_secrets",
            "legacy_codegen": None,
            "legacy_agents": None,
            "runtime_can_read_secrets": False,
            "vault_can_read_secrets": True,
        }
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_codegen_routing_requires_an_explicit_vault_consumer_grant() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    project_id = _project_id("vaultcodegen")
    actor_user_id: uuid.UUID | None = None
    connection_id: uuid.UUID | None = None
    try:
        async with pool.acquire() as conn:
            actor_user_id = await _create_project(conn, project_id)
            async with conn.transaction():
                connection_id, _ = await _seed_codegen_projection(
                    conn, project_id, actor_user_id
                )
        await assign_project_models(
            pool,
            project_id=project_id,
            editor_provider="openai",
            editor_model_id="gpt-5.4-mini",
            helper_provider="openai",
            helper_model_id="gpt-5.4-mini",
            actor="test:vault-routing",
        )
        assert await _project_llm_assignments(pool, project_id) == []

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO llm_vault_connection_consumers (
                    connection_id, project_id, provider, consumer,
                    granted_by_actor_user_id
                ) VALUES ($1, $2, 'openai', 'codegen', $3)
                """,
                connection_id,
                project_id,
                actor_user_id,
            )
        assignments = await _project_llm_assignments(pool, project_id)
        assert [assignment.role for assignment in assignments] == [
            "editor",
            "helper",
        ]
        assert {assignment.provider for assignment in assignments} == {"openai"}
    finally:
        if actor_user_id is not None and connection_id is not None:
            async with pool.acquire() as conn:
                await _delete_project_fixture(
                    conn,
                    project_id=project_id,
                    connection_id=connection_id,
                )
        await pool.close()


@pytest.mark.asyncio
async def test_vault_audit_is_immutable() -> None:
    assert POSTGRES_URL is not None
    conn = await asyncpg.connect(POSTGRES_URL)
    project_id = _project_id("vaultaudit")
    try:
        with pytest.raises(
            asyncpg.CheckViolationError,
            match="LLM vault audit rows are immutable",
        ):
            async with conn.transaction():
                actor_user_id = await _create_project(conn, project_id)
                connection_id, credential_id = await _seed_codegen_projection(
                    conn, project_id, actor_user_id
                )
                await conn.execute(
                    """
                    INSERT INTO llm_vault_connection_consumers (
                        connection_id, project_id, provider, consumer,
                        granted_by_actor_user_id
                    ) VALUES ($1, $2, 'openai', 'codegen', $3)
                    """,
                    connection_id,
                    project_id,
                    actor_user_id,
                )
                audit_id = await conn.fetchval(
                    """
                    INSERT INTO llm_vault_audit (
                        connection_id, project_id, provider, credential_id,
                        credential_version, action, outcome, consumers,
                        actor_user_id
                    ) VALUES (
                        $1, $2, 'openai', $3, 1, 'create', 'succeeded',
                        ARRAY['codegen']::TEXT[], $4
                    ) RETURNING audit_id
                    """,
                    connection_id,
                    project_id,
                    credential_id,
                    actor_user_id,
                )
                await conn.execute(
                    "DELETE FROM llm_vault_audit WHERE audit_id = $1",
                    audit_id,
                )
    finally:
        await conn.close()
