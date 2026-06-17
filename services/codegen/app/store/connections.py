"""Persistence for repo connections (``codegen_connections``)."""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.models.connection import Connection, ConnectionCreate


def _loads(value: Any) -> dict[str, Any]:
    """Coerce a JSONB column (str from asyncpg, dict from fakes) to a dict."""
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _row_to_connection(row: asyncpg.Record) -> Connection:
    return Connection(
        project_id=row["project_id"],
        installation_id=row["installation_id"],
        repo=row["repo"],
        default_base_branch=row["default_base_branch"],
        policy=_loads(row["policy"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def upsert_connection(pool: asyncpg.Pool, payload: ConnectionCreate) -> Connection:
    """Create or update the repo binding for a project (one binding per project)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO codegen_connections
                (project_id, installation_id, repo, default_base_branch, policy)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (project_id) DO UPDATE SET
                installation_id = EXCLUDED.installation_id,
                repo = EXCLUDED.repo,
                default_base_branch = EXCLUDED.default_base_branch,
                policy = EXCLUDED.policy,
                updated_at = now()
            RETURNING *
            """,
            payload.project_id,
            payload.installation_id,
            payload.repo,
            payload.default_base_branch,
            json.dumps(payload.policy),
        )
    return _row_to_connection(row)


async def get_connection(pool: asyncpg.Pool, project_id: str) -> Connection | None:
    """Return the repo binding for a project, or ``None`` if unconnected."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM codegen_connections WHERE project_id = $1",
            project_id,
        )
    return _row_to_connection(row) if row else None
