"""Atomic Codegen project LLM connections and normalized model inventories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from app.llm.provider_catalog import CATALOG_VERSION, ProviderModel
from app.store.llm_credentials import (
    CredentialNotFoundError,
    DecryptedCredential,
    ProjectCredentialStore,
    Provider,
    canonical_provider,
)


ConnectionState = Literal["active", "revoked"]


class LlmConnectionError(RuntimeError):
    """Base class for secret-free connection lifecycle failures."""


class LlmConnectionNotFoundError(LlmConnectionError):
    """The requested active project/provider connection does not exist."""


class LlmConnectionConflictError(LlmConnectionError):
    """The optimistic connection version changed."""


class LlmConnectionAssignmentConflictError(LlmConnectionError):
    """A connection mutation would invalidate an assigned model."""


class LlmConnectionAuthorizationError(LlmConnectionError):
    """The current human actor lacks live connection-management authority."""


@dataclass(frozen=True)
class ConnectionMetadata:
    project_id: str
    provider: Provider
    version: int
    inventory_version: int
    state: ConnectionState
    credential_id: UUID
    catalog_version: str
    validated_at: datetime
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None
    model_count: int


def _connection(row: Any) -> ConnectionMetadata:
    return ConnectionMetadata(
        project_id=str(row["project_id"]),
        provider=cast(Provider, str(row["provider"])),
        version=int(row["version"]),
        inventory_version=int(row["inventory_version"]),
        state=cast(ConnectionState, str(row["state"])),
        credential_id=UUID(str(row["credential_id"])),
        catalog_version=str(row["catalog_version"]),
        validated_at=row["validated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revoked_at=row["revoked_at"],
        model_count=int(row["model_count"]),
    )


class ProjectConnectionStore:
    """Project/provider connection operations with live human authorization."""

    def __init__(self, pool: Any, credential_store: ProjectCredentialStore) -> None:
        self._pool = pool
        self._credentials = credential_store

    @staticmethod
    async def _assert_authority(
        conn: Any,
        project_id: str,
        actor_user_id: UUID,
        *,
        lock: bool,
    ) -> None:
        suffix = "FOR UPDATE OF project, account" if lock else ""
        row = await conn.fetchrow(
            f"""
            SELECT project.owner_user_id, account.active
            FROM admin_projects AS project
            JOIN admin_users AS account ON account.user_id = $2
            WHERE project.project_id = $1
            {suffix}
            """,
            project_id,
            actor_user_id,
        )
        if row is None or not bool(row["active"]):
            raise LlmConnectionAuthorizationError(
                "Connection management authority is unavailable"
            )
        if row["owner_user_id"] == actor_user_id:
            return
        roles = await conn.fetchval(
            """
            SELECT roles
            FROM admin_user_projects
            WHERE project_id = $1 AND user_id = $2
            FOR SHARE
            """,
            project_id,
            actor_user_id,
        )
        if not {"agents:manage", "credentials:manage"} <= {
            str(role) for role in (roles or [])
        }:
            raise LlmConnectionAuthorizationError(
                "Connection management requires project ownership or delegated "
                "agents:manage and credentials:manage roles"
            )

    async def assert_mutation_authority(
        self, project_id: str, actor_user_id: UUID
    ) -> None:
        async with self._pool.acquire() as conn:
            await self._assert_authority(
                conn, project_id, actor_user_id, lock=False
            )

    @staticmethod
    async def _lock_connection(
        conn: Any, project_id: str, provider: Provider
    ) -> Any:
        await ProjectCredentialStore.lock_pair(conn, project_id, provider)
        return await conn.fetchrow(
            """
            SELECT connection.*, (
                SELECT count(*)
                FROM codegen_project_provider_models AS model
                WHERE model.project_id = connection.project_id
                  AND model.provider = connection.provider
            ) AS model_count
            FROM codegen_project_provider_connections AS connection
            WHERE connection.project_id = $1 AND connection.provider = $2
            FOR UPDATE
            """,
            project_id,
            provider,
        )

    @staticmethod
    async def _assert_assignments_remain_available(
        conn: Any,
        project_id: str,
        provider: Provider,
        models: tuple[ProviderModel, ...],
    ) -> None:
        assignments = await conn.fetch(
            """
            SELECT role, model_id
            FROM codegen_project_model_assignments
            WHERE project_id = $1 AND provider = $2
            FOR SHARE
            """,
            project_id,
            provider,
        )
        eligible = {
            (role, model.model_id)
            for model in models
            for role in model.supported_roles
        }
        missing = [
            f"{row['role']}:{row['model_id']}"
            for row in assignments
            if (str(row["role"]), str(row["model_id"])) not in eligible
        ]
        if missing:
            raise LlmConnectionAssignmentConflictError(
                "Discovered inventory omits assigned role/model(s): "
                + ", ".join(sorted(missing))
            )

    @staticmethod
    async def _insert_inventory(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        connection_version: int,
        inventory_version: int,
        models: tuple[ProviderModel, ...],
    ) -> None:
        for model in models:
            await conn.execute(
                """
                INSERT INTO codegen_project_provider_models (
                    project_id, provider, connection_version, inventory_version,
                    schema_version, model_id, display_name, supported_roles,
                    catalog_version, context_window_tokens,
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
                connection_version,
                inventory_version,
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

    @staticmethod
    async def _advance_assignments(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        connection_version: int,
        inventory_version: int,
        actor: str,
    ) -> None:
        await conn.execute(
            """
            UPDATE codegen_project_model_assignments
            SET connection_version = $3, inventory_version = $4,
                catalog_version = $5,
                assignment_version = assignment_version + 1,
                assigned_by_actor = $6,
                assigned_at = NOW()
            WHERE project_id = $1 AND provider = $2
            """,
            project_id,
            provider,
            connection_version,
            inventory_version,
            CATALOG_VERSION,
            actor,
        )

    @staticmethod
    async def _append_audit(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        action: Literal["connect", "replace", "refresh", "revoke"],
        version: int,
        inventory_version: int,
        credential_id: UUID,
        actor_user_id: UUID,
        model_count: int,
    ) -> None:
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
            inventory_version,
            credential_id,
            actor_user_id,
            model_count,
            CATALOG_VERSION,
        )

    async def put(
        self,
        project_id: str,
        provider: str,
        api_key: str,
        models: tuple[ProviderModel, ...],
        *,
        expected_version: int,
        actor_user_id: UUID,
    ) -> ConnectionMetadata:
        canonical = canonical_provider(provider)
        actor = f"user:{actor_user_id}"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, project_id, actor_user_id, lock=True
                )
                current = await self._lock_connection(
                    conn, project_id, canonical
                )
                version = int(current["version"]) if current else 0
                if version != expected_version:
                    raise LlmConnectionConflictError(
                        "The provider connection version changed"
                    )
                await self._assert_assignments_remain_available(
                    conn, project_id, canonical, models
                )
                if current is not None and str(current["state"]) == "active":
                    credential = await self._credentials.replace_in_transaction(
                        conn,
                        project_id,
                        canonical,
                        api_key,
                        expected_credential_id=UUID(
                            str(current["credential_id"])
                        ),
                        actor=actor,
                    )
                    action: Literal["connect", "replace"] = "replace"
                else:
                    credential = await self._credentials.create_in_transaction(
                        conn, project_id, canonical, api_key, actor=actor
                    )
                    action = "connect"
                next_version = version + 1
                inventory_version = (
                    int(current["inventory_version"]) + 1 if current else 1
                )
                if current is None:
                    await conn.execute(
                        """
                        INSERT INTO codegen_project_provider_connections (
                            project_id, provider, version, inventory_version,
                            state, credential_id, catalog_version, validated_at,
                            validated_by_actor
                        ) VALUES (
                            $1, $2, $3, $4, 'active', $5, $6, NOW(), $7
                        )
                        """,
                        project_id,
                        canonical,
                        next_version,
                        inventory_version,
                        credential.credential_id,
                        CATALOG_VERSION,
                        actor,
                    )
                else:
                    await conn.execute(
                        """
                        DELETE FROM codegen_project_provider_models
                        WHERE project_id = $1 AND provider = $2
                        """,
                        project_id,
                        canonical,
                    )
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_connections
                        SET version = $3, inventory_version = $4,
                            state = 'active', credential_id = $5,
                            catalog_version = $6, validated_at = NOW(),
                            validated_by_actor = $7, updated_at = NOW(),
                            revoked_at = NULL
                        WHERE project_id = $1 AND provider = $2
                        """,
                        project_id,
                        canonical,
                        next_version,
                        inventory_version,
                        credential.credential_id,
                        CATALOG_VERSION,
                        actor,
                    )
                await self._insert_inventory(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    connection_version=next_version,
                    inventory_version=inventory_version,
                    models=models,
                )
                await self._advance_assignments(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    connection_version=next_version,
                    inventory_version=inventory_version,
                    actor=actor,
                )
                await self._append_audit(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    action=action,
                    version=next_version,
                    inventory_version=inventory_version,
                    credential_id=credential.credential_id,
                    actor_user_id=actor_user_id,
                    model_count=len(models),
                )
                row = await self._fetch_one(conn, project_id, canonical)
        assert row is not None
        return _connection(row)

    async def credential_for_refresh(
        self,
        project_id: str,
        provider: str,
        *,
        expected_version: int,
    ) -> DecryptedCredential:
        canonical = canonical_provider(provider)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT version, state, credential_id
                FROM codegen_project_provider_connections
                WHERE project_id = $1 AND provider = $2
                """,
                project_id,
                canonical,
            )
        if (
            row is None
            or str(row["state"]) != "active"
            or int(row["version"]) != expected_version
        ):
            raise LlmConnectionConflictError(
                "The provider connection version changed"
            )
        try:
            return await self._credentials.load_active(
                project_id,
                canonical,
                credential_id=UUID(str(row["credential_id"])),
            )
        except CredentialNotFoundError as exc:
            raise LlmConnectionConflictError(
                "The provider credential changed"
            ) from exc

    async def refresh(
        self,
        project_id: str,
        provider: str,
        models: tuple[ProviderModel, ...],
        *,
        expected_version: int,
        expected_credential_id: UUID,
        actor_user_id: UUID,
    ) -> ConnectionMetadata:
        canonical = canonical_provider(provider)
        actor = f"user:{actor_user_id}"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, project_id, actor_user_id, lock=True
                )
                current = await self._lock_connection(
                    conn, project_id, canonical
                )
                if (
                    current is None
                    or str(current["state"]) != "active"
                    or int(current["version"]) != expected_version
                    or UUID(str(current["credential_id"]))
                    != expected_credential_id
                ):
                    raise LlmConnectionConflictError(
                        "The provider connection version changed"
                    )
                active = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM codegen_project_provider_credentials
                        WHERE credential_id = $1 AND project_id = $2
                          AND provider = $3 AND state = 'active'
                    )
                    """,
                    expected_credential_id,
                    project_id,
                    canonical,
                )
                if not active:
                    raise LlmConnectionConflictError(
                        "The provider credential changed"
                    )
                await self._assert_assignments_remain_available(
                    conn, project_id, canonical, models
                )
                next_version = expected_version + 1
                inventory_version = int(current["inventory_version"]) + 1
                await conn.execute(
                    """
                    DELETE FROM codegen_project_provider_models
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical,
                )
                await conn.execute(
                    """
                    UPDATE codegen_project_provider_connections
                    SET version = $3, inventory_version = $4,
                        catalog_version = $5, validated_at = NOW(),
                        validated_by_actor = $6, updated_at = NOW()
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical,
                    next_version,
                    inventory_version,
                    CATALOG_VERSION,
                    actor,
                )
                await self._insert_inventory(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    connection_version=next_version,
                    inventory_version=inventory_version,
                    models=models,
                )
                await self._advance_assignments(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    connection_version=next_version,
                    inventory_version=inventory_version,
                    actor=actor,
                )
                await self._append_audit(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    action="refresh",
                    version=next_version,
                    inventory_version=inventory_version,
                    credential_id=expected_credential_id,
                    actor_user_id=actor_user_id,
                    model_count=len(models),
                )
                row = await self._fetch_one(conn, project_id, canonical)
        assert row is not None
        return _connection(row)

    async def revoke(
        self,
        project_id: str,
        provider: str,
        *,
        expected_version: int,
        actor_user_id: UUID,
    ) -> ConnectionMetadata:
        canonical = canonical_provider(provider)
        actor = f"user:{actor_user_id}"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn, project_id, actor_user_id, lock=True
                )
                current = await self._lock_connection(
                    conn, project_id, canonical
                )
                if (
                    current is None
                    or str(current["state"]) != "active"
                    or int(current["version"]) != expected_version
                ):
                    raise LlmConnectionConflictError(
                        "The provider connection version changed"
                    )
                assigned = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM codegen_project_model_assignments
                        WHERE project_id = $1 AND provider = $2
                    )
                    """,
                    project_id,
                    canonical,
                )
                if assigned:
                    raise LlmConnectionAssignmentConflictError(
                        "Provider connection is referenced by a model assignment"
                    )
                credential_id = UUID(str(current["credential_id"]))
                await self._credentials.revoke_in_transaction(
                    conn,
                    project_id,
                    canonical,
                    expected_credential_id=credential_id,
                    actor=actor,
                )
                next_version = expected_version + 1
                inventory_version = int(current["inventory_version"]) + 1
                await conn.execute(
                    """
                    DELETE FROM codegen_project_provider_models
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical,
                )
                await conn.execute(
                    """
                    UPDATE codegen_project_provider_connections
                    SET version = $3, inventory_version = $4,
                        state = 'revoked', updated_at = NOW(), revoked_at = NOW()
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical,
                    next_version,
                    inventory_version,
                )
                await self._append_audit(
                    conn,
                    project_id=project_id,
                    provider=canonical,
                    action="revoke",
                    version=next_version,
                    inventory_version=inventory_version,
                    credential_id=credential_id,
                    actor_user_id=actor_user_id,
                    model_count=0,
                )
                row = await self._fetch_one(conn, project_id, canonical)
        assert row is not None
        return _connection(row)

    @staticmethod
    async def _fetch_one(
        conn: Any, project_id: str, provider: Provider
    ) -> Any:
        return await conn.fetchrow(
            """
            SELECT connection.*, (
                SELECT count(*)
                FROM codegen_project_provider_models AS model
                WHERE model.project_id = connection.project_id
                  AND model.provider = connection.provider
            ) AS model_count
            FROM codegen_project_provider_connections AS connection
            WHERE connection.project_id = $1 AND connection.provider = $2
            """,
            project_id,
            provider,
        )

    async def list(self, project_id: str) -> tuple[ConnectionMetadata, ...]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT connection.*, (
                    SELECT count(*)
                    FROM codegen_project_provider_models AS model
                    WHERE model.project_id = connection.project_id
                      AND model.provider = connection.provider
                ) AS model_count
                FROM codegen_project_provider_connections AS connection
                WHERE connection.project_id = $1
                ORDER BY connection.provider
                """,
                project_id,
            )
        return tuple(_connection(row) for row in rows)

    @staticmethod
    def _models(rows: list[Any]) -> tuple[ProviderModel, ...]:
        return tuple(
            ProviderModel(
                schema_version="codegen_provider_model@1",
                provider=cast(Provider, str(row["provider"])),
                model_id=str(row["model_id"]),
                display_name=str(row["display_name"]),
                supported_roles=tuple(row["supported_roles"]),
                catalog_version=str(row["catalog_version"]),
                context_window_tokens=int(row["context_window_tokens"]),
                supports_tool_calling=bool(row["supports_tool_calling"]),
                supports_structured_output=bool(
                    row["supports_structured_output"]
                ),
                data_residency=row["data_residency"],
                allowed_data_classifications=tuple(
                    row["allowed_data_classifications"]
                ),
                input_cost_per_million_tokens_usd_micros=int(
                    row["input_cost_per_million_tokens_usd_micros"]
                ),
                output_cost_per_million_tokens_usd_micros=int(
                    row["output_cost_per_million_tokens_usd_micros"]
                ),
                pricing_status="catalog_reviewed",
            )
            for row in rows
        )

    async def get_active_with_models(
        self, project_id: str, provider: str
    ) -> tuple[ConnectionMetadata, tuple[ProviderModel, ...]]:
        canonical = canonical_provider(provider)
        async with self._pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read", readonly=True
            ):
                connection_row = await self._fetch_one(
                    conn, project_id, canonical
                )
                if (
                    connection_row is None
                    or str(connection_row["state"]) != "active"
                ):
                    raise LlmConnectionNotFoundError(
                        "Provider connection not found"
                    )
                rows = await conn.fetch(
                    """
                    SELECT model.*
                    FROM codegen_project_provider_models AS model
                    WHERE model.project_id = $1 AND model.provider = $2
                      AND model.connection_version = $3
                      AND model.inventory_version = $4
                    ORDER BY model.model_id
                    """,
                    project_id,
                    canonical,
                    int(connection_row["version"]),
                    int(connection_row["inventory_version"]),
                )
                if not rows:
                    raise LlmConnectionConflictError(
                        "Provider model inventory is unavailable"
                    )
                connection = _connection(connection_row)
                models = self._models(rows)
        return connection, models

    async def models(
        self, project_id: str, provider: str
    ) -> tuple[ProviderModel, ...]:
        _, models = await self.get_active_with_models(project_id, provider)
        return models
