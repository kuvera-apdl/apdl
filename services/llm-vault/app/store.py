"""Transactional vault lifecycle, consumer projections, and JIT access."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from app.contracts import (
    AgentsProjection,
    CodegenProjection,
    ConnectionDetail,
    ConnectionList,
    ConnectionSummary,
    Consumer,
    CredentialAccessRequest,
    CredentialAccessResponse,
    Provider,
    VaultProviderModel,
)
from app.crypto import CredentialCipher


class VaultStoreError(RuntimeError):
    """Base class for secret-free vault lifecycle failures."""


class VaultNotFoundError(VaultStoreError):
    """The requested connection or exact credential authority is absent."""


class VaultConflictError(VaultStoreError):
    """Optimistic version, binding, or assignment authority changed."""


class VaultAuthorizationError(VaultStoreError):
    """The human actor no longer has live project credential authority."""


@dataclass(frozen=True)
class ConnectionAuthority:
    connection_id: UUID
    project_id: str
    provider: Provider
    version: int
    credential_id: UUID
    credential_version: int
    consumers: tuple[Consumer, ...]


@dataclass(frozen=True)
class RefreshAuthority(ConnectionAuthority):
    api_key: str = field(repr=False)


Projection = AgentsProjection | CodegenProjection


def _consumers(values: Any) -> tuple[Consumer, ...]:
    present = {str(value) for value in values}
    return cast(
        tuple[Consumer, ...],
        tuple(item for item in ("agents", "codegen") if item in present),
    )


class ProjectLlmVaultStore:
    def __init__(self, pool: Any, cipher: CredentialCipher) -> None:
        self._pool = pool
        self._cipher = cipher

    @staticmethod
    async def _assert_authority(
        conn: Any,
        project_id: str,
        actor_user_id: UUID,
        *,
        lock: bool,
    ) -> None:
        del lock
        authority = await conn.fetchval(
            "SELECT apdl_project_management_authority($1, $2)",
            project_id,
            actor_user_id,
        )
        if str(authority) not in {"owner", "delegated"}:
            raise VaultAuthorizationError(
                "Connection management requires project ownership or delegated "
                "agents:manage and credentials:manage roles"
            )

    @staticmethod
    async def _assert_read_authority(
        conn: Any, project_id: str, actor_user_id: UUID
    ) -> None:
        row = await conn.fetchrow(
            """
            SELECT project.owner_user_id, account.active,
                   membership.roles
            FROM admin_projects AS project
            JOIN admin_users AS account ON account.user_id = $2
            LEFT JOIN admin_user_projects AS membership
              ON membership.project_id = project.project_id
             AND membership.user_id = account.user_id
            WHERE project.project_id = $1
            """,
            project_id,
            actor_user_id,
        )
        if row is None or not bool(row["active"]):
            raise VaultAuthorizationError(
                "Project credential authority is unavailable"
            )
        if row["owner_user_id"] == actor_user_id:
            return
        if "agents:read" not in {
            str(role) for role in (row["roles"] or [])
        }:
            raise VaultAuthorizationError(
                "Connection visibility requires the agents:read role"
            )

    @staticmethod
    async def _lock_scope(conn: Any, project_id: str, provider: Provider) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"apdl:llm-vault:{project_id}:{provider}",
        )

    @staticmethod
    async def _current_connection(conn: Any, connection_id: UUID) -> Any:
        return await conn.fetchrow(
            """
            SELECT connection.*, credential.credential_id,
                   credential.credential_version
            FROM llm_vault_connections AS connection
            LEFT JOIN llm_vault_provider_credentials AS credential
              ON credential.connection_id = connection.connection_id
             AND credential.state = 'active'
            WHERE connection.connection_id = $1
            FOR UPDATE OF connection
            """,
            connection_id,
        )

    @staticmethod
    async def _connection_response(
        conn: Any, connection_id: UUID, *, detail: bool
    ) -> ConnectionSummary | ConnectionDetail:
        row = await conn.fetchrow(
            """
            SELECT connection.*,
                   COALESCE(array_agg(consumer.consumer ORDER BY consumer.consumer)
                       FILTER (WHERE consumer.consumer IS NOT NULL), ARRAY[]::TEXT[])
                       AS consumers,
                   (
                       SELECT COUNT(*)
                       FROM llm_vault_provider_models AS model
                       WHERE model.connection_id = connection.connection_id
                         AND model.inventory_version = connection.inventory_version
                   ) AS model_count
            FROM llm_vault_connections AS connection
            LEFT JOIN llm_vault_connection_consumers AS consumer
              ON consumer.connection_id = connection.connection_id
            WHERE connection.connection_id = $1
            GROUP BY connection.connection_id
            """,
            connection_id,
        )
        if row is None:
            raise VaultNotFoundError("Project LLM connection was not found")
        values = dict(
            connection_id=UUID(str(row["connection_id"])),
            project_id=str(row["project_id"]),
            provider=str(row["provider"]),
            label=str(row["label"]),
            version=int(row["version"]),
            inventory_version=int(row["inventory_version"]),
            state=str(row["state"]),
            consumers=_consumers(row["consumers"]),
            validated_at=row["validated_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revoked_at=row["revoked_at"],
            model_count=int(row["model_count"]),
        )
        if not detail:
            return ConnectionSummary(**values)
        model_rows = await conn.fetch(
            """
            SELECT model_id
            FROM llm_vault_provider_models
            WHERE connection_id = $1 AND inventory_version = $2
            ORDER BY model_id
            """,
            connection_id,
            int(row["inventory_version"]),
        )
        return ConnectionDetail(
            **values,
            models=tuple(
                VaultProviderModel(model_id=str(model["model_id"]))
                for model in model_rows
            ),
        )

    async def list(
        self, project_id: str, actor_user_id: UUID
    ) -> ConnectionList:
        async with self._pool.acquire() as conn:
            await self._assert_read_authority(conn, project_id, actor_user_id)
            ids = await conn.fetch(
                """
                SELECT connection_id
                FROM llm_vault_connections
                WHERE project_id = $1
                ORDER BY provider, label, connection_id
                """,
                project_id,
            )
            connections: list[ConnectionSummary] = []
            for row in ids:
                connection = await self._connection_response(
                    conn, UUID(str(row["connection_id"])), detail=False
                )
                connections.append(cast(ConnectionSummary, connection))
        return ConnectionList(project_id=project_id, connections=tuple(connections))

    async def get(
        self, connection_id: UUID, project_id: str, actor_user_id: UUID
    ) -> ConnectionDetail:
        async with self._pool.acquire() as conn:
            await self._assert_read_authority(conn, project_id, actor_user_id)
            result = await self._connection_response(conn, connection_id, detail=True)
        if result.project_id != project_id:
            raise VaultNotFoundError("Project LLM connection was not found")
        return cast(ConnectionDetail, result)

    @staticmethod
    async def _assert_binding_available(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        consumer: Consumer,
        connection_id: UUID | None,
    ) -> None:
        bound = await conn.fetchval(
            """
            SELECT connection_id
            FROM llm_vault_connection_consumers
            WHERE project_id = $1 AND provider = $2 AND consumer = $3
            FOR SHARE
            """,
            project_id,
            provider,
            consumer,
        )
        if bound is not None and UUID(str(bound)) != connection_id:
            raise VaultConflictError(
                f"{consumer} already uses another {provider} connection"
            )

    @staticmethod
    async def _assert_projection_safe(
        conn: Any,
        *,
        consumer: Consumer,
        project_id: str,
        provider: Provider,
        projection: Projection | None,
    ) -> None:
        if projection is None:
            assigned = await conn.fetchval(
                (
                    "SELECT EXISTS (SELECT 1 FROM llm_project_model_assignments "
                    "WHERE project_id = $1 AND provider = $2)"
                    if consumer == "agents"
                    else "SELECT EXISTS (SELECT 1 FROM "
                    "codegen_project_model_assignments "
                    "WHERE project_id = $1 AND provider = $2)"
                ),
                project_id,
                provider,
            )
            if assigned:
                raise VaultConflictError(
                    f"{consumer} model assignments must be changed before removing access"
                )
            return
        if consumer == "agents":
            assert isinstance(projection, AgentsProjection)
            eligible = {
                (tier, model.model_id)
                for model in projection.models
                for tier in model.supported_tiers
            }
            rows = await conn.fetch(
                """
                SELECT tier, model
                FROM llm_project_model_assignments
                WHERE project_id = $1 AND provider = $2
                """,
                project_id,
                provider,
            )
            missing = [
                f"{row['tier']}:{row['model']}"
                for row in rows
                if (str(row["tier"]), str(row["model"])) not in eligible
            ]
        else:
            assert isinstance(projection, CodegenProjection)
            eligible = {
                (role, model.model_id)
                for model in projection.models
                for role in model.supported_roles
            }
            rows = await conn.fetch(
                """
                SELECT role, model_id
                FROM codegen_project_model_assignments
                WHERE project_id = $1 AND provider = $2
                """,
                project_id,
                provider,
            )
            missing = [
                f"{row['role']}:{row['model_id']}"
                for row in rows
                if (str(row["role"]), str(row["model_id"])) not in eligible
            ]
        if missing:
            raise VaultConflictError(
                f"{consumer} projection omits assigned model(s): "
                + ", ".join(sorted(missing))
            )

    @staticmethod
    async def _project_agents(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        credential_id: UUID,
        actor_user_id: UUID,
        projection: AgentsProjection,
        action: Literal["connect", "replace", "refresh"],
    ) -> None:
        current = await conn.fetchrow(
            """
            SELECT version, inventory_version
            FROM llm_project_provider_connections
            WHERE project_id = $1 AND provider = $2
            FOR UPDATE
            """,
            project_id,
            provider,
        )
        version = (int(current["version"]) if current else 0) + 1
        inventory = (int(current["inventory_version"]) if current else 0) + 1
        actor = f"user:{actor_user_id}"
        if current is None:
            await conn.execute(
                """
                INSERT INTO llm_project_provider_connections (
                    project_id, provider, version, inventory_version, state,
                    credential_id, catalog_version, validated_at,
                    validated_by_actor
                ) VALUES ($1, $2, $3, $4, 'active', $5, $6, NOW(), $7)
                """,
                project_id,
                provider,
                version,
                inventory,
                credential_id,
                projection.catalog_version,
                actor,
            )
        else:
            await conn.execute(
                "DELETE FROM llm_project_provider_models "
                "WHERE project_id = $1 AND provider = $2",
                project_id,
                provider,
            )
            await conn.execute(
                """
                UPDATE llm_project_provider_connections
                SET version = $3, inventory_version = $4, state = 'active',
                    credential_id = $5, catalog_version = $6,
                    validated_at = NOW(), validated_by_actor = $7,
                    updated_at = NOW(), revoked_at = NULL
                WHERE project_id = $1 AND provider = $2
                """,
                project_id,
                provider,
                version,
                inventory,
                credential_id,
                projection.catalog_version,
                actor,
            )
        for model in projection.models:
            await conn.execute(
                """
                INSERT INTO llm_project_provider_models (
                    project_id, provider, connection_version,
                    inventory_version, schema_version, model_id, display_name,
                    supported_tiers, catalog_version, data_residency,
                    allowed_data_classifications, pricing_status, discovered_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    NOW()
                )
                """,
                project_id,
                provider,
                version,
                inventory,
                model.schema_version,
                model.model_id,
                model.display_name,
                list(model.supported_tiers),
                model.catalog_version,
                model.data_residency,
                list(model.allowed_data_classifications),
                model.pricing_status,
            )
        await conn.execute(
            """
            INSERT INTO llm_project_provider_connection_audit (
                project_id, provider, action, outcome, connection_version,
                credential_id, actor_user_id, model_count, catalog_version
            ) VALUES ($1, $2, $3, 'succeeded', $4, $5, $6, $7, $8)
            """,
            project_id,
            provider,
            action,
            version,
            credential_id,
            actor_user_id,
            len(projection.models),
            projection.catalog_version,
        )

    @staticmethod
    async def _project_codegen(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        credential_id: UUID,
        actor_user_id: UUID,
        projection: CodegenProjection,
        action: Literal["connect", "replace", "refresh"],
    ) -> None:
        current = await conn.fetchrow(
            """
            SELECT version, inventory_version
            FROM codegen_project_provider_connections
            WHERE project_id = $1 AND provider = $2
            FOR UPDATE
            """,
            project_id,
            provider,
        )
        version = (int(current["version"]) if current else 0) + 1
        inventory = (int(current["inventory_version"]) if current else 0) + 1
        actor = f"user:{actor_user_id}"
        if current is None:
            await conn.execute(
                """
                INSERT INTO codegen_project_provider_connections (
                    project_id, provider, version, inventory_version, state,
                    credential_id, catalog_version, validated_at,
                    validated_by_actor
                ) VALUES ($1, $2, $3, $4, 'active', $5, $6, NOW(), $7)
                """,
                project_id,
                provider,
                version,
                inventory,
                credential_id,
                projection.catalog_version,
                actor,
            )
        else:
            await conn.execute(
                "DELETE FROM codegen_project_provider_models "
                "WHERE project_id = $1 AND provider = $2",
                project_id,
                provider,
            )
            await conn.execute(
                """
                UPDATE codegen_project_provider_connections
                SET version = $3, inventory_version = $4, state = 'active',
                    credential_id = $5, catalog_version = $6,
                    validated_at = NOW(), validated_by_actor = $7,
                    updated_at = NOW(), revoked_at = NULL
                WHERE project_id = $1 AND provider = $2
                """,
                project_id,
                provider,
                version,
                inventory,
                credential_id,
                projection.catalog_version,
                actor,
            )
        for model in projection.models:
            await conn.execute(
                """
                INSERT INTO codegen_project_provider_models (
                    project_id, provider, connection_version,
                    inventory_version, schema_version, model_id, display_name,
                    supported_roles, catalog_version, context_window_tokens,
                    supports_tool_calling, supports_structured_output,
                    data_residency, allowed_data_classifications,
                    input_cost_per_million_tokens_usd_micros,
                    output_cost_per_million_tokens_usd_micros,
                    pricing_status, discovered_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                    $13, $14, $15, $16, $17, NOW()
                )
                """,
                project_id,
                provider,
                version,
                inventory,
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
        await conn.execute(
            """
            UPDATE codegen_project_model_assignments
            SET connection_version = $3, inventory_version = $4,
                catalog_version = $5,
                assignment_version = assignment_version + 1,
                assigned_by_actor = $6, assigned_at = NOW()
            WHERE project_id = $1 AND provider = $2
            """,
            project_id,
            provider,
            version,
            inventory,
            projection.catalog_version,
            actor,
        )
        await conn.execute(
            """
            INSERT INTO codegen_project_provider_connection_audit (
                project_id, provider, action, outcome, connection_version,
                inventory_version, credential_id, actor_user_id, model_count,
                catalog_version
            ) VALUES ($1, $2, $3, 'succeeded', $4, $5, $6, $7, $8, $9)
            """,
            project_id,
            provider,
            action,
            version,
            inventory,
            credential_id,
            actor_user_id,
            len(projection.models),
            projection.catalog_version,
        )

    @staticmethod
    async def _revoke_projection(
        conn: Any,
        *,
        consumer: Consumer,
        project_id: str,
        provider: Provider,
        credential_id: UUID,
        actor_user_id: UUID,
    ) -> None:
        await ProjectLlmVaultStore._assert_projection_safe(
            conn,
            consumer=consumer,
            project_id=project_id,
            provider=provider,
            projection=None,
        )
        connection_table = (
            "llm_project_provider_connections"
            if consumer == "agents"
            else "codegen_project_provider_connections"
        )
        model_table = (
            "llm_project_provider_models"
            if consumer == "agents"
            else "codegen_project_provider_models"
        )
        current = await conn.fetchrow(
            f"SELECT version, inventory_version FROM {connection_table} "
            "WHERE project_id = $1 AND provider = $2 FOR UPDATE",
            project_id,
            provider,
        )
        if current is None:
            return
        version = int(current["version"]) + 1
        inventory = int(current["inventory_version"]) + 1
        await conn.execute(
            f"DELETE FROM {model_table} WHERE project_id = $1 AND provider = $2",
            project_id,
            provider,
        )
        await conn.execute(
            f"UPDATE {connection_table} SET version = $3, "
            "inventory_version = $4, state = 'revoked', updated_at = NOW(), "
            "revoked_at = NOW() WHERE project_id = $1 AND provider = $2",
            project_id,
            provider,
            version,
            inventory,
        )
        if consumer == "agents":
            catalog = await conn.fetchval(
                "SELECT catalog_version FROM llm_project_provider_connections "
                "WHERE project_id = $1 AND provider = $2",
                project_id,
                provider,
            )
            await conn.execute(
                """
                INSERT INTO llm_project_provider_connection_audit (
                    project_id, provider, action, outcome, connection_version,
                    credential_id, actor_user_id, model_count, catalog_version
                ) VALUES ($1, $2, 'revoke', 'succeeded', $3, $4, $5, 0, $6)
                """,
                project_id,
                provider,
                version,
                credential_id,
                actor_user_id,
                catalog,
            )
        else:
            catalog = await conn.fetchval(
                "SELECT catalog_version FROM codegen_project_provider_connections "
                "WHERE project_id = $1 AND provider = $2",
                project_id,
                provider,
            )
            await conn.execute(
                """
                INSERT INTO codegen_project_provider_connection_audit (
                    project_id, provider, action, outcome, connection_version,
                    inventory_version, credential_id, actor_user_id, model_count,
                    catalog_version
                ) VALUES ($1, $2, 'revoke', 'succeeded', $3, $4, $5, $6, 0, $7)
                """,
                project_id,
                provider,
                version,
                inventory,
                credential_id,
                actor_user_id,
                catalog,
            )

    async def create(
        self,
        *,
        project_id: str,
        provider: Provider,
        label: str,
        api_key: str,
        consumers: tuple[Consumer, ...],
        model_ids: tuple[str, ...],
        projections: dict[Consumer, Projection],
        actor_user_id: UUID,
    ) -> ConnectionDetail:
        connection_id = uuid4()
        credential_id = uuid4()
        encrypted = self._cipher.encrypt(
            api_key,
            credential_id=credential_id,
            connection_id=connection_id,
            project_id=project_id,
            provider=provider,
            credential_version=1,
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, project_id, actor_user_id, lock=True
                )
                await self._lock_scope(conn, project_id, provider)
                for consumer in consumers:
                    await self._assert_binding_available(
                        conn,
                        project_id=project_id,
                        provider=provider,
                        consumer=consumer,
                        connection_id=None,
                    )
                    await self._assert_projection_safe(
                        conn,
                        consumer=consumer,
                        project_id=project_id,
                        provider=provider,
                        projection=projections[consumer],
                    )
                await conn.execute(
                    """
                    INSERT INTO llm_vault_connections (
                        connection_id, project_id, provider, label, version,
                        inventory_version, state, validated_at,
                        created_by_actor_user_id
                    ) VALUES ($1, $2, $3, $4, 1, 1, 'active', NOW(), $5)
                    """,
                    connection_id,
                    project_id,
                    provider,
                    label,
                    actor_user_id,
                )
                await self._insert_credential(
                    conn,
                    credential_id=credential_id,
                    connection_id=connection_id,
                    project_id=project_id,
                    provider=provider,
                    version=1,
                    actor_user_id=actor_user_id,
                    encrypted=encrypted,
                )
                await self._replace_consumers(
                    conn,
                    connection_id=connection_id,
                    project_id=project_id,
                    provider=provider,
                    consumers=consumers,
                    actor_user_id=actor_user_id,
                )
                await self._replace_models(
                    conn, connection_id, 1, 1, model_ids
                )
                for consumer in consumers:
                    await self._apply_projection(
                        conn,
                        consumer=consumer,
                        project_id=project_id,
                        provider=provider,
                        credential_id=credential_id,
                        actor_user_id=actor_user_id,
                        projection=projections[consumer],
                        action="connect",
                    )
                await self._audit(
                    conn,
                    connection_id=connection_id,
                    project_id=project_id,
                    provider=provider,
                    credential_id=credential_id,
                    credential_version=1,
                    action="create",
                    consumers=consumers,
                    actor_user_id=actor_user_id,
                )
                response = await self._connection_response(
                    conn, connection_id, detail=True
                )
        return cast(ConnectionDetail, response)

    @staticmethod
    async def _insert_credential(
        conn: Any,
        *,
        credential_id: UUID,
        connection_id: UUID,
        project_id: str,
        provider: Provider,
        version: int,
        actor_user_id: UUID,
        encrypted: Any,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO llm_vault_provider_credentials (
                credential_id, connection_id, project_id, provider,
                credential_version, state, created_by_actor_user_id
            ) VALUES ($1, $2, $3, $4, $5, 'active', $6)
            """,
            credential_id,
            connection_id,
            project_id,
            provider,
            version,
            actor_user_id,
        )
        await conn.execute(
            """
            INSERT INTO llm_vault_provider_secrets (
                credential_id, ciphertext, nonce, algorithm, schema_version,
                encryption_key_id
            ) VALUES (
                $1, $2, $3, 'AES-256-GCM',
                'llm_vault_provider_secret@1', $4
            )
            """,
            credential_id,
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.encryption_key_id,
        )

    @staticmethod
    async def _replace_consumers(
        conn: Any,
        *,
        connection_id: UUID,
        project_id: str,
        provider: Provider,
        consumers: tuple[Consumer, ...],
        actor_user_id: UUID,
    ) -> None:
        await conn.execute(
            "DELETE FROM llm_vault_connection_consumers WHERE connection_id = $1",
            connection_id,
        )
        for consumer in consumers:
            await conn.execute(
                """
                INSERT INTO llm_vault_connection_consumers (
                    connection_id, project_id, provider, consumer,
                    granted_by_actor_user_id
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                connection_id,
                project_id,
                provider,
                consumer,
                actor_user_id,
            )

    @staticmethod
    async def _replace_models(
        conn: Any,
        connection_id: UUID,
        connection_version: int,
        inventory_version: int,
        model_ids: tuple[str, ...],
    ) -> None:
        await conn.execute(
            "DELETE FROM llm_vault_provider_models WHERE connection_id = $1",
            connection_id,
        )
        for model_id in model_ids:
            await conn.execute(
                """
                INSERT INTO llm_vault_provider_models (
                    connection_id, connection_version, inventory_version,
                    model_id, discovered_at
                ) VALUES ($1, $2, $3, $4, NOW())
                """,
                connection_id,
                connection_version,
                inventory_version,
                model_id,
            )

    @staticmethod
    async def _apply_projection(
        conn: Any,
        *,
        consumer: Consumer,
        project_id: str,
        provider: Provider,
        credential_id: UUID,
        actor_user_id: UUID,
        projection: Projection,
        action: Literal["connect", "replace", "refresh"],
    ) -> None:
        if consumer == "agents":
            assert isinstance(projection, AgentsProjection)
            await ProjectLlmVaultStore._project_agents(
                conn,
                project_id=project_id,
                provider=provider,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                projection=projection,
                action=action,
            )
        else:
            assert isinstance(projection, CodegenProjection)
            await ProjectLlmVaultStore._project_codegen(
                conn,
                project_id=project_id,
                provider=provider,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                projection=projection,
                action=action,
            )

    @staticmethod
    async def _audit(
        conn: Any,
        *,
        connection_id: UUID,
        project_id: str,
        provider: Provider,
        credential_id: UUID,
        credential_version: int,
        action: Literal["create", "replace", "refresh", "revoke"],
        consumers: tuple[Consumer, ...],
        actor_user_id: UUID,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO llm_vault_audit (
                connection_id, project_id, provider, credential_id,
                credential_version, action, outcome, consumers, actor_user_id
            ) VALUES ($1, $2, $3, $4, $5, $6, 'succeeded', $7, $8)
            """,
            connection_id,
            project_id,
            provider,
            credential_id,
            credential_version,
            action,
            list(consumers),
            actor_user_id,
        )

    async def load_mutation_authority(
        self,
        *,
        connection_id: UUID,
        project_id: str,
        expected_version: int,
        actor_user_id: UUID,
    ) -> ConnectionAuthority:
        async with self._pool.acquire() as conn:
            await self._assert_authority(
                conn, project_id, actor_user_id, lock=False
            )
            row = await conn.fetchrow(
                """
                SELECT connection.connection_id, connection.project_id,
                       connection.provider, connection.version,
                       credential.credential_id,
                       credential.credential_version,
                       array_agg(consumer.consumer ORDER BY consumer.consumer)
                           AS consumers
                FROM llm_vault_connections AS connection
                JOIN llm_vault_provider_credentials AS credential
                  ON credential.connection_id = connection.connection_id
                 AND credential.state = 'active'
                JOIN llm_vault_connection_consumers AS consumer
                  ON consumer.connection_id = connection.connection_id
                WHERE connection.connection_id = $1
                  AND connection.project_id = $2
                  AND connection.version = $3
                  AND connection.state = 'active'
                GROUP BY connection.connection_id, credential.credential_id,
                         credential.credential_version
                """,
                connection_id,
                project_id,
                expected_version,
            )
        if row is None:
            raise VaultConflictError("Project LLM connection version changed")
        return ConnectionAuthority(
            connection_id=connection_id,
            project_id=project_id,
            provider=cast(Provider, str(row["provider"])),
            version=expected_version,
            credential_id=UUID(str(row["credential_id"])),
            credential_version=int(row["credential_version"]),
            consumers=_consumers(row["consumers"]),
        )

    async def load_refresh_authority(
        self,
        *,
        connection_id: UUID,
        project_id: str,
        expected_version: int,
        actor_user_id: UUID,
    ) -> RefreshAuthority:
        authority = await self.load_mutation_authority(
            connection_id=connection_id,
            project_id=project_id,
            expected_version=expected_version,
            actor_user_id=actor_user_id,
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ciphertext, nonce, encryption_key_id
                FROM llm_vault_provider_secrets
                WHERE credential_id = $1
                """,
                authority.credential_id,
            )
        if row is None:
            raise VaultConflictError("Project LLM connection version changed")
        api_key = self._cipher.decrypt(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            encryption_key_id=str(row["encryption_key_id"]),
            credential_id=authority.credential_id,
            connection_id=connection_id,
            project_id=project_id,
            provider=authority.provider,
            credential_version=authority.credential_version,
        )
        return RefreshAuthority(
            connection_id=authority.connection_id,
            project_id=authority.project_id,
            provider=authority.provider,
            version=authority.version,
            credential_id=authority.credential_id,
            credential_version=authority.credential_version,
            consumers=authority.consumers,
            api_key=api_key,
        )

    async def replace(
        self,
        *,
        authority: ConnectionAuthority,
        label: str,
        api_key: str,
        consumers: tuple[Consumer, ...],
        model_ids: tuple[str, ...],
        projections: dict[Consumer, Projection],
        actor_user_id: UUID,
    ) -> ConnectionDetail:
        new_credential_id = uuid4()
        new_version = authority.credential_version + 1
        encrypted = self._cipher.encrypt(
            api_key,
            credential_id=new_credential_id,
            connection_id=authority.connection_id,
            project_id=authority.project_id,
            provider=authority.provider,
            credential_version=new_version,
        )
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, authority.project_id, actor_user_id, lock=True
                )
                await self._lock_scope(
                    conn, authority.project_id, authority.provider
                )
                current = await self._current_connection(
                    conn, authority.connection_id
                )
                if (
                    current is None
                    or str(current["state"]) != "active"
                    or int(current["version"]) != authority.version
                    or UUID(str(current["credential_id"]))
                    != authority.credential_id
                ):
                    raise VaultConflictError(
                        "Project LLM connection version changed"
                    )
                for consumer in consumers:
                    await self._assert_binding_available(
                        conn,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        consumer=consumer,
                        connection_id=authority.connection_id,
                    )
                    await self._assert_projection_safe(
                        conn,
                        consumer=consumer,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        projection=projections[consumer],
                    )
                removed = tuple(
                    consumer
                    for consumer in authority.consumers
                    if consumer not in consumers
                )
                for consumer in removed:
                    await self._revoke_projection(
                        conn,
                        consumer=consumer,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        credential_id=authority.credential_id,
                        actor_user_id=actor_user_id,
                    )
                await conn.execute(
                    """
                    UPDATE llm_vault_provider_credentials
                    SET state = 'replaced', successor_credential_id = $2,
                        retired_by_actor_user_id = $3,
                        retirement_reason = 'Provider connection replaced',
                        retired_at = NOW()
                    WHERE credential_id = $1 AND state = 'active'
                    """,
                    authority.credential_id,
                    new_credential_id,
                    actor_user_id,
                )
                await conn.execute(
                    "DELETE FROM llm_vault_provider_secrets "
                    "WHERE credential_id = $1",
                    authority.credential_id,
                )
                await self._insert_credential(
                    conn,
                    credential_id=new_credential_id,
                    connection_id=authority.connection_id,
                    project_id=authority.project_id,
                    provider=authority.provider,
                    version=new_version,
                    actor_user_id=actor_user_id,
                    encrypted=encrypted,
                )
                next_connection_version = authority.version + 1
                next_inventory_version = int(current["inventory_version"]) + 1
                await conn.execute(
                    """
                    UPDATE llm_vault_connections
                    SET label = $2, version = $3, inventory_version = $4,
                        validated_at = NOW(), updated_at = NOW()
                    WHERE connection_id = $1
                    """,
                    authority.connection_id,
                    label,
                    next_connection_version,
                    next_inventory_version,
                )
                await self._replace_consumers(
                    conn,
                    connection_id=authority.connection_id,
                    project_id=authority.project_id,
                    provider=authority.provider,
                    consumers=consumers,
                    actor_user_id=actor_user_id,
                )
                await self._replace_models(
                    conn,
                    authority.connection_id,
                    next_connection_version,
                    next_inventory_version,
                    model_ids,
                )
                for consumer in consumers:
                    await self._apply_projection(
                        conn,
                        consumer=consumer,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        credential_id=new_credential_id,
                        actor_user_id=actor_user_id,
                        projection=projections[consumer],
                        action="replace",
                    )
                await self._audit(
                    conn,
                    connection_id=authority.connection_id,
                    project_id=authority.project_id,
                    provider=authority.provider,
                    credential_id=new_credential_id,
                    credential_version=new_version,
                    action="replace",
                    consumers=consumers,
                    actor_user_id=actor_user_id,
                )
                response = await self._connection_response(
                    conn, authority.connection_id, detail=True
                )
        return cast(ConnectionDetail, response)

    async def refresh(
        self,
        *,
        authority: RefreshAuthority,
        model_ids: tuple[str, ...],
        projections: dict[Consumer, Projection],
        actor_user_id: UUID,
    ) -> ConnectionDetail:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, authority.project_id, actor_user_id, lock=True
                )
                await self._lock_scope(
                    conn, authority.project_id, authority.provider
                )
                current = await self._current_connection(
                    conn, authority.connection_id
                )
                if (
                    current is None
                    or int(current["version"]) != authority.version
                    or UUID(str(current["credential_id"]))
                    != authority.credential_id
                ):
                    raise VaultConflictError(
                        "Project LLM connection version changed"
                    )
                for consumer in authority.consumers:
                    await self._assert_projection_safe(
                        conn,
                        consumer=consumer,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        projection=projections[consumer],
                    )
                next_version = authority.version + 1
                next_inventory = int(current["inventory_version"]) + 1
                await conn.execute(
                    """
                    UPDATE llm_vault_connections
                    SET version = $2, inventory_version = $3,
                        validated_at = NOW(), updated_at = NOW()
                    WHERE connection_id = $1
                    """,
                    authority.connection_id,
                    next_version,
                    next_inventory,
                )
                await self._replace_models(
                    conn,
                    authority.connection_id,
                    next_version,
                    next_inventory,
                    model_ids,
                )
                for consumer in authority.consumers:
                    await self._apply_projection(
                        conn,
                        consumer=consumer,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        credential_id=authority.credential_id,
                        actor_user_id=actor_user_id,
                        projection=projections[consumer],
                        action="refresh",
                    )
                await self._audit(
                    conn,
                    connection_id=authority.connection_id,
                    project_id=authority.project_id,
                    provider=authority.provider,
                    credential_id=authority.credential_id,
                    credential_version=authority.credential_version,
                    action="refresh",
                    consumers=authority.consumers,
                    actor_user_id=actor_user_id,
                )
                response = await self._connection_response(
                    conn, authority.connection_id, detail=True
                )
        return cast(ConnectionDetail, response)

    async def revoke(
        self,
        *,
        authority: ConnectionAuthority,
        reason: str,
        actor_user_id: UUID,
    ) -> ConnectionSummary:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, authority.project_id, actor_user_id, lock=True
                )
                await self._lock_scope(
                    conn, authority.project_id, authority.provider
                )
                current = await self._current_connection(
                    conn, authority.connection_id
                )
                if (
                    current is None
                    or int(current["version"]) != authority.version
                    or UUID(str(current["credential_id"]))
                    != authority.credential_id
                ):
                    raise VaultConflictError(
                        "Project LLM connection version changed"
                    )
                for consumer in authority.consumers:
                    await self._revoke_projection(
                        conn,
                        consumer=consumer,
                        project_id=authority.project_id,
                        provider=authority.provider,
                        credential_id=authority.credential_id,
                        actor_user_id=actor_user_id,
                    )
                await conn.execute(
                    "DELETE FROM llm_vault_connection_consumers "
                    "WHERE connection_id = $1",
                    authority.connection_id,
                )
                await conn.execute(
                    "DELETE FROM llm_vault_provider_models "
                    "WHERE connection_id = $1",
                    authority.connection_id,
                )
                await conn.execute(
                    "DELETE FROM llm_vault_provider_secrets "
                    "WHERE credential_id = $1",
                    authority.credential_id,
                )
                await conn.execute(
                    """
                    UPDATE llm_vault_provider_credentials
                    SET state = 'revoked', retired_by_actor_user_id = $2,
                        retirement_reason = $3, retired_at = NOW()
                    WHERE credential_id = $1 AND state = 'active'
                    """,
                    authority.credential_id,
                    actor_user_id,
                    reason,
                )
                await conn.execute(
                    """
                    UPDATE llm_vault_connections
                    SET version = version + 1, inventory_version = inventory_version + 1,
                        state = 'revoked', updated_at = NOW(),
                        revoked_by_actor_user_id = $2,
                        revocation_reason = $3, revoked_at = NOW()
                    WHERE connection_id = $1
                    """,
                    authority.connection_id,
                    actor_user_id,
                    reason,
                )
                await self._audit(
                    conn,
                    connection_id=authority.connection_id,
                    project_id=authority.project_id,
                    provider=authority.provider,
                    credential_id=authority.credential_id,
                    credential_version=authority.credential_version,
                    action="revoke",
                    consumers=authority.consumers,
                    actor_user_id=actor_user_id,
                )
                response = await self._connection_response(
                    conn, authority.connection_id, detail=False
                )
        return cast(ConnectionSummary, response)

    async def issue_access(
        self, request: CredentialAccessRequest
    ) -> CredentialAccessResponse:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    SELECT connection.connection_id,
                           credential.credential_id,
                           credential.credential_version,
                           secret.ciphertext, secret.nonce,
                           secret.encryption_key_id
                    FROM llm_vault_connections AS connection
                    JOIN llm_vault_connection_consumers AS consumer
                      ON consumer.connection_id = connection.connection_id
                     AND consumer.project_id = connection.project_id
                     AND consumer.provider = connection.provider
                     AND consumer.consumer = $3
                    JOIN llm_vault_provider_credentials AS credential
                      ON credential.connection_id = connection.connection_id
                     AND credential.project_id = connection.project_id
                     AND credential.provider = connection.provider
                     AND credential.state = 'active'
                    JOIN llm_vault_provider_secrets AS secret
                      ON secret.credential_id = credential.credential_id
                    WHERE connection.project_id = $1
                      AND connection.provider = $2
                      AND connection.state = 'active'
                      AND credential.credential_id = $4
                      AND credential.credential_version = $5
                    FOR SHARE OF connection, credential, secret, consumer
                    """,
                    request.project_id,
                    request.provider,
                    request.consumer,
                    request.expected_credential_id,
                    request.expected_credential_version,
                )
                if row is None:
                    raise VaultNotFoundError(
                        "Exact project credential authority is unavailable"
                    )
                connection_id = UUID(str(row["connection_id"]))
                credential_id = UUID(str(row["credential_id"]))
                credential_version = int(row["credential_version"])
                api_key = self._cipher.decrypt(
                    ciphertext=bytes(row["ciphertext"]),
                    nonce=bytes(row["nonce"]),
                    encryption_key_id=str(row["encryption_key_id"]),
                    credential_id=credential_id,
                    connection_id=connection_id,
                    project_id=request.project_id,
                    provider=request.provider,
                    credential_version=credential_version,
                )
                access_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO llm_vault_access_audit (
                        access_id, connection_id, project_id, provider,
                        credential_id, credential_version, consumer,
                        execution_id, purpose, outcome
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'issued')
                    """,
                    access_id,
                    connection_id,
                    request.project_id,
                    request.provider,
                    credential_id,
                    credential_version,
                    request.consumer,
                    request.execution_id,
                    request.purpose,
                )
        return CredentialAccessResponse(
            access_id=access_id,
            connection_id=connection_id,
            credential_id=credential_id,
            credential_version=credential_version,
            provider=request.provider,
            api_key=api_key,
        )
