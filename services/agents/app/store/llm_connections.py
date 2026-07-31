"""Atomic project LLM connection, model inventory, and authority storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from app.llm.provider_catalog import CATALOG_VERSION, ProviderModel, catalog_model
from app.store.llm_credentials import (
    DecryptedCredential,
    ProjectCredentialStore,
    REMOTE_PROVIDERS,
    RemoteProvider,
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
    provider: RemoteProvider
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


def _provider(provider: str) -> RemoteProvider:
    if provider not in REMOTE_PROVIDERS:
        raise ValueError("provider must be openai, anthropic, google, or xai")
    return cast(RemoteProvider, provider)


def _connection(row: Any) -> ConnectionMetadata:
    return ConnectionMetadata(
        project_id=str(row["project_id"]),
        provider=cast(RemoteProvider, str(row["provider"])),
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

    def __init__(
        self,
        pool: Any,
        credential_store: ProjectCredentialStore,
    ) -> None:
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
        effective_roles = {str(role) for role in (roles or [])}
        if not {"agents:manage", "credentials:manage"} <= effective_roles:
            raise LlmConnectionAuthorizationError(
                "Connection management requires project ownership or delegated "
                "agents:manage and credentials:manage roles"
            )

    async def assert_mutation_authority(
        self,
        project_id: str,
        actor_user_id: UUID,
    ) -> None:
        async with self._pool.acquire() as conn:
            await self._assert_authority(
                conn,
                project_id,
                actor_user_id,
                lock=False,
            )

    @staticmethod
    async def _lock_connection(
        conn: Any,
        project_id: str,
        provider: RemoteProvider,
    ) -> Any:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"apdl:llm-credential:{project_id}:{provider}",
        )
        return await conn.fetchrow(
            """
            SELECT connection.*, (
                SELECT count(*)
                FROM llm_project_provider_models AS model
                WHERE model.project_id = connection.project_id
                  AND model.provider = connection.provider
            ) AS model_count
            FROM llm_project_provider_connections AS connection
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
        provider: RemoteProvider,
        models: tuple[ProviderModel, ...],
    ) -> None:
        assignments = await conn.fetch(
            """
            SELECT tier, model
            FROM llm_project_model_assignments
            WHERE project_id = $1 AND provider = $2
            FOR SHARE
            """,
            project_id,
            provider,
        )
        discovered = {model.model_id for model in models}
        missing = [
            f"{row['tier']}:{row['model']}"
            for row in assignments
            if str(row["model"]) not in discovered
        ]
        state = await conn.fetchval(
            """
            SELECT state
            FROM llm_project_policies
            WHERE project_id = $1
            FOR SHARE
            """,
            project_id,
        )
        if missing and state == "active":
            raise LlmConnectionAssignmentConflictError(
                "Discovered inventory omits assigned model(s): "
                + ", ".join(sorted(missing))
            )

    @staticmethod
    async def _insert_inventory(
        conn: Any,
        *,
        project_id: str,
        provider: RemoteProvider,
        connection_version: int,
        inventory_version: int,
        models: tuple[ProviderModel, ...],
    ) -> None:
        for model in models:
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
                connection_version,
                inventory_version,
                model.schema_version,
                model.model_id,
                model.display_name,
                list(model.supported_tiers),
                model.catalog_version,
                model.data_residency,
                list(model.allowed_data_classifications),
                model.pricing_status,
            )

    @staticmethod
    async def _append_audit(
        conn: Any,
        *,
        project_id: str,
        provider: RemoteProvider,
        action: Literal["connect", "replace", "refresh", "revoke"],
        version: int,
        credential_id: UUID,
        actor_user_id: UUID,
        model_count: int,
    ) -> None:
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
        canonical_provider = _provider(provider)
        actor = f"user:{actor_user_id}"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn,
                    project_id,
                    actor_user_id,
                    lock=True,
                )
                current = await self._lock_connection(
                    conn,
                    project_id,
                    canonical_provider,
                )
                current_version = int(current["version"]) if current else 0
                current_inventory_version = (
                    int(current["inventory_version"]) if current else 0
                )
                if current_version != expected_version:
                    raise LlmConnectionConflictError(
                        "The provider connection version changed"
                    )
                await self._assert_assignments_remain_available(
                    conn,
                    project_id,
                    canonical_provider,
                    models,
                )
                if current is not None and str(current["state"]) == "active":
                    credential = await self._credentials.replace_in_transaction(
                        conn,
                        project_id,
                        canonical_provider,
                        api_key,
                        expected_credential_id=UUID(
                            str(current["credential_id"])
                        ),
                        actor=actor,
                        reason="Provider connection replaced",
                    )
                    action: Literal["connect", "replace"] = "replace"
                else:
                    credential = await self._credentials.create_in_transaction(
                        conn,
                        project_id,
                        canonical_provider,
                        api_key,
                        actor=actor,
                    )
                    action = "connect"
                next_version = current_version + 1
                next_inventory_version = current_inventory_version + 1
                if current is None:
                    await conn.execute(
                        """
                        INSERT INTO llm_project_provider_connections (
                            project_id, provider, version, inventory_version,
                            state, credential_id, catalog_version, validated_at,
                            validated_by_actor
                        ) VALUES (
                            $1, $2, $3, $4, 'active', $5, $6, NOW(), $7
                        )
                        """,
                        project_id,
                        canonical_provider,
                        next_version,
                        next_inventory_version,
                        credential.credential_id,
                        CATALOG_VERSION,
                        actor,
                    )
                else:
                    await conn.execute(
                        """
                        DELETE FROM llm_project_provider_models
                        WHERE project_id = $1 AND provider = $2
                        """,
                        project_id,
                        canonical_provider,
                    )
                    await conn.execute(
                        """
                        UPDATE llm_project_provider_connections
                        SET version = $3, inventory_version = $4,
                            state = 'active', credential_id = $5,
                            catalog_version = $6, validated_at = NOW(),
                            validated_by_actor = $7, updated_at = NOW(),
                            revoked_at = NULL
                        WHERE project_id = $1 AND provider = $2
                        """,
                        project_id,
                        canonical_provider,
                        next_version,
                        next_inventory_version,
                        credential.credential_id,
                        CATALOG_VERSION,
                        actor,
                    )
                await self._insert_inventory(
                    conn,
                    project_id=project_id,
                    provider=canonical_provider,
                    connection_version=next_version,
                    inventory_version=next_inventory_version,
                    models=models,
                )
                await self._append_audit(
                    conn,
                    project_id=project_id,
                    provider=canonical_provider,
                    action=action,
                    version=next_version,
                    credential_id=credential.credential_id,
                    actor_user_id=actor_user_id,
                    model_count=len(models),
                )
                row = await self._fetch_one(
                    conn,
                    project_id,
                    canonical_provider,
                )
        assert row is not None
        return _connection(row)

    async def credential_for_refresh(
        self,
        project_id: str,
        provider: str,
        *,
        expected_version: int,
    ) -> DecryptedCredential:
        canonical_provider = _provider(provider)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT version, state, credential_id
                FROM llm_project_provider_connections
                WHERE project_id = $1 AND provider = $2
                """,
                project_id,
                canonical_provider,
            )
        if (
            row is None
            or str(row["state"]) != "active"
            or int(row["version"]) != expected_version
        ):
            raise LlmConnectionConflictError(
                "The provider connection version changed"
            )
        return await self._credentials.load_active(
            project_id,
            canonical_provider,
            credential_id=UUID(str(row["credential_id"])),
        )

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
        canonical_provider = _provider(provider)
        actor = f"user:{actor_user_id}"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn,
                    project_id,
                    actor_user_id,
                    lock=True,
                )
                current = await self._lock_connection(
                    conn,
                    project_id,
                    canonical_provider,
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
                credential_active = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM llm_project_provider_credentials
                        WHERE credential_id = $1 AND project_id = $2
                          AND provider = $3 AND state = 'active'
                    )
                    """,
                    expected_credential_id,
                    project_id,
                    canonical_provider,
                )
                if not credential_active:
                    raise LlmConnectionConflictError(
                        "The provider credential changed"
                    )
                await self._assert_assignments_remain_available(
                    conn,
                    project_id,
                    canonical_provider,
                    models,
                )
                next_version = expected_version + 1
                next_inventory_version = int(current["inventory_version"]) + 1
                await conn.execute(
                    """
                    DELETE FROM llm_project_provider_models
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical_provider,
                )
                await conn.execute(
                    """
                    UPDATE llm_project_provider_connections
                    SET version = $3, inventory_version = $4,
                        catalog_version = $5,
                        validated_at = NOW(), validated_by_actor = $6,
                        updated_at = NOW()
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical_provider,
                    next_version,
                    next_inventory_version,
                    CATALOG_VERSION,
                    actor,
                )
                await self._insert_inventory(
                    conn,
                    project_id=project_id,
                    provider=canonical_provider,
                    connection_version=next_version,
                    inventory_version=next_inventory_version,
                    models=models,
                )
                await self._append_audit(
                    conn,
                    project_id=project_id,
                    provider=canonical_provider,
                    action="refresh",
                    version=next_version,
                    credential_id=expected_credential_id,
                    actor_user_id=actor_user_id,
                    model_count=len(models),
                )
                row = await self._fetch_one(
                    conn,
                    project_id,
                    canonical_provider,
                )
        assert row is not None
        return _connection(row)

    async def revoke(
        self,
        project_id: str,
        provider: str,
        *,
        expected_version: int,
        actor_user_id: UUID,
        reason: str,
    ) -> ConnectionMetadata:
        canonical_provider = _provider(provider)
        actor = f"user:{actor_user_id}"
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await self._assert_authority(
                    conn,
                    project_id,
                    actor_user_id,
                    lock=True,
                )
                current = await self._lock_connection(
                    conn,
                    project_id,
                    canonical_provider,
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
                        FROM llm_project_model_assignments
                        WHERE project_id = $1 AND provider = $2
                    )
                    """,
                    project_id,
                    canonical_provider,
                )
                if assigned:
                    raise LlmConnectionAssignmentConflictError(
                        "Provider connection is referenced by a model assignment"
                    )
                credential_id = UUID(str(current["credential_id"]))
                await self._credentials.revoke_in_transaction(
                    conn,
                    project_id,
                    canonical_provider,
                    expected_credential_id=credential_id,
                    actor=actor,
                    reason=reason,
                )
                next_version = expected_version + 1
                await conn.execute(
                    """
                    DELETE FROM llm_project_provider_models
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical_provider,
                )
                await conn.execute(
                    """
                    UPDATE llm_project_provider_connections
                    SET version = $3, state = 'revoked', updated_at = NOW(),
                        revoked_at = NOW()
                    WHERE project_id = $1 AND provider = $2
                    """,
                    project_id,
                    canonical_provider,
                    next_version,
                )
                await self._append_audit(
                    conn,
                    project_id=project_id,
                    provider=canonical_provider,
                    action="revoke",
                    version=next_version,
                    credential_id=credential_id,
                    actor_user_id=actor_user_id,
                    model_count=0,
                )
                row = await self._fetch_one(
                    conn,
                    project_id,
                    canonical_provider,
                )
        assert row is not None
        return _connection(row)

    @staticmethod
    async def _fetch_one(
        conn: Any,
        project_id: str,
        provider: RemoteProvider,
    ) -> Any:
        return await conn.fetchrow(
            """
            SELECT connection.*, (
                SELECT count(*)
                FROM llm_project_provider_models AS model
                WHERE model.project_id = connection.project_id
                  AND model.provider = connection.provider
            ) AS model_count
            FROM llm_project_provider_connections AS connection
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
                    FROM llm_project_provider_models AS model
                    WHERE model.project_id = connection.project_id
                      AND model.provider = connection.provider
                ) AS model_count
                FROM llm_project_provider_connections AS connection
                WHERE connection.project_id = $1
                ORDER BY connection.provider
                """,
                project_id,
            )
        return tuple(_connection(row) for row in rows)

    @staticmethod
    def _models(rows: list[Any]) -> tuple[ProviderModel, ...]:
        models: list[ProviderModel] = []
        for row in rows:
            model = catalog_model(str(row["provider"]), str(row["model_id"]))
            if model is None or model.catalog_version != str(
                row["catalog_version"]
            ):
                raise LlmConnectionConflictError(
                    "Provider model inventory uses a stale catalog"
                )
            models.append(model)
        return tuple(models)

    async def get_active_with_models(
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ConnectionMetadata, tuple[ProviderModel, ...]]:
        canonical_provider = _provider(provider)
        async with self._pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                connection_row = await self._fetch_one(
                    conn,
                    project_id,
                    canonical_provider,
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
                    FROM llm_project_provider_models AS model
                    WHERE model.project_id = $1 AND model.provider = $2
                      AND model.connection_version = $3
                      AND model.inventory_version = $4
                    ORDER BY model.model_id
                    """,
                    project_id,
                    canonical_provider,
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
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ProviderModel, ...]:
        """Return the current inventory through one consistent snapshot."""
        _, models = await self.get_active_with_models(project_id, provider)
        return models
