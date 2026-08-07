"""Read-only Agents projections of project LLM connections and models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from app.llm.provider_catalog import ProviderModel, catalog_model
from app.store.llm_credentials import REMOTE_PROVIDERS, RemoteProvider


ConnectionState = Literal["active", "revoked"]


class LlmConnectionError(RuntimeError):
    """Base class for connection projection failures."""


class LlmConnectionNotFoundError(LlmConnectionError):
    """The requested active project/provider connection does not exist."""


class LlmConnectionConflictError(LlmConnectionError):
    """The projected connection or model inventory is inconsistent."""


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
    """Read the Agents projection maintained exclusively by the LLM vault."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

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
