"""Persistence for repo connections (``codegen_connections``)."""

from __future__ import annotations

import json

import asyncpg

from app.models.connection import Connection, ConnectionCreate
from app.safety.policy import TenantCodegenConnectionPolicy
from app.store.jsonb import loads_jsonb


def _row_to_connection(row: asyncpg.Record) -> Connection:
    return Connection(
        project_id=row["project_id"],
        installation_id=row["installation_id"],
        repo=row["repo"],
        default_base_branch=row["default_base_branch"],
        tenant_policy=TenantCodegenConnectionPolicy.model_validate(
            loads_jsonb(row["tenant_policy"])
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert_connection(pool: asyncpg.Pool, payload: ConnectionCreate) -> Connection:
    """Create or update only the repo binding for a project.

    Tenant policy has a separate authority-checked endpoint. On conflict this
    upsert deliberately leaves the existing policy untouched.
    """
    default_policy = TenantCodegenConnectionPolicy().model_dump(mode="json")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO codegen_connections
                (project_id, installation_id, repo, default_base_branch, tenant_policy)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (project_id) DO UPDATE SET
                installation_id = EXCLUDED.installation_id,
                repo = EXCLUDED.repo,
                default_base_branch = EXCLUDED.default_base_branch,
                updated_at = now()
            RETURNING *
            """,
            payload.project_id,
            payload.installation_id,
            payload.repo,
            payload.default_base_branch,
            json.dumps(default_policy),
        )
    return _row_to_connection(row)


async def get_tenant_policy(
    pool: asyncpg.Pool, project_id: str
) -> TenantCodegenConnectionPolicy | None:
    """Return a project's strict tenant policy, or ``None`` if unconnected."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_policy
            FROM codegen_connections
            WHERE project_id = $1
            """,
            project_id,
        )
    if row is None:
        return None
    return TenantCodegenConnectionPolicy.model_validate(
        loads_jsonb(row["tenant_policy"])
    )


async def replace_tenant_policy(
    pool: asyncpg.Pool,
    project_id: str,
    policy: TenantCodegenConnectionPolicy,
) -> TenantCodegenConnectionPolicy | None:
    """Completely replace a project's tenant-owned policy document."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE codegen_connections
            SET tenant_policy = $2::jsonb, updated_at = now()
            WHERE project_id = $1
            RETURNING tenant_policy
            """,
            project_id,
            json.dumps(policy.model_dump(mode="json")),
        )
    if row is None:
        return None
    return TenantCodegenConnectionPolicy.model_validate(
        loads_jsonb(row["tenant_policy"])
    )


async def get_connection(pool: asyncpg.Pool, project_id: str) -> Connection | None:
    """Return the repo binding for a project, or ``None`` if unconnected."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM codegen_connections WHERE project_id = $1",
            project_id,
        )
    return _row_to_connection(row) if row else None


async def delete_connection(pool: asyncpg.Pool, project_id: str) -> bool:
    """Remove the repo binding for a project. Returns ``False`` if none existed."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM codegen_connections WHERE project_id = $1 RETURNING project_id",
            project_id,
        )
    return row is not None
