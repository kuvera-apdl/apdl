"""Read-only Codegen projections of project LLM connections and models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from app.llm.provider_catalog import ProviderModel
from app.store.llm_credentials import Provider, canonical_provider


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
    """Read the Codegen projection maintained exclusively by the LLM vault."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @staticmethod
    async def _fetch_one(
        conn: Any,
        project_id: str,
        provider: Provider,
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
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ConnectionMetadata, tuple[ProviderModel, ...]]:
        canonical = canonical_provider(provider)
        async with self._pool.acquire() as conn:
            async with conn.transaction(
                isolation="repeatable_read",
                readonly=True,
            ):
                connection_row = await self._fetch_one(
                    conn,
                    project_id,
                    canonical,
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
        self,
        project_id: str,
        provider: str,
    ) -> tuple[ProviderModel, ...]:
        _, models = await self.get_active_with_models(project_id, provider)
        return models
