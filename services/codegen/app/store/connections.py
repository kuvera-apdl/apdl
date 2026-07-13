"""Persistence for verified repository grants and project connections."""

from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg

from app.models.connection import (
    Connection,
    ConnectionCreate,
    RepositoryGrant,
    RepositoryTarget,
)
from app.safety.policy import TenantCodegenConnectionPolicy
from app.store.jsonb import loads_jsonb

_CONNECTION_SELECT = """
    SELECT
        connection.project_id,
        connection.grant_id,
        grant_record.installation_id,
        grant_record.repository_id,
        grant_record.repository_full_name,
        connection.default_base_branch,
        connection.tenant_policy,
        connection.created_at,
        connection.updated_at
    FROM codegen_connections AS connection
    JOIN github_repository_grants AS grant_record
      ON grant_record.project_id = connection.project_id
     AND grant_record.grant_id = connection.grant_id
    WHERE connection.project_id = $1
      AND grant_record.status = 'active'
      AND grant_record.verified_at IS NOT NULL
      AND grant_record.revoked_at IS NULL
"""


def _row_to_connection(row: asyncpg.Record | dict[str, Any]) -> Connection:
    target = RepositoryTarget(
        grant_id=row["grant_id"],
        project_id=row["project_id"],
        installation_id=row["installation_id"],
        repository_id=row["repository_id"],
        repository_full_name=row["repository_full_name"],
    )
    connection = Connection(
        project_id=row["project_id"],
        grant_id=row["grant_id"],
        repository_id=row["repository_id"],
        repository_full_name=row["repository_full_name"],
        default_base_branch=row["default_base_branch"],
        tenant_policy=TenantCodegenConnectionPolicy.model_validate(
            loads_jsonb(row["tenant_policy"])
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    return connection.attach_target(target)


def _row_to_repository_grant(
    row: asyncpg.Record | dict[str, Any],
) -> RepositoryGrant:
    return RepositoryGrant.model_validate(dict(row))


async def _fetch_active_connection(
    conn: asyncpg.Connection, project_id: str
) -> Connection | None:
    row = await conn.fetchrow(_CONNECTION_SELECT, project_id)
    return _row_to_connection(row) if row else None


async def upsert_connection(pool: asyncpg.Pool, payload: ConnectionCreate) -> Connection:
    """Bind a project to an already active, same-project repository grant.

    The strict input contains no GitHub coordinates.  Tenant policy has a
    separate authority-checked endpoint and remains untouched when a verified
    grant is rebound.
    """
    default_policy = TenantCodegenConnectionPolicy().model_dump(mode="json")
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO codegen_connections
                    (project_id, grant_id, default_base_branch, tenant_policy)
                SELECT $1, $2, $3, $4::jsonb
                FROM github_repository_grants AS grant_record
                WHERE grant_record.project_id = $1
                  AND grant_record.grant_id = $2
                  AND grant_record.status = 'active'
                  AND grant_record.verified_at IS NOT NULL
                  AND grant_record.revoked_at IS NULL
                ON CONFLICT (project_id) DO UPDATE SET
                    grant_id = EXCLUDED.grant_id,
                    default_base_branch = EXCLUDED.default_base_branch,
                    updated_at = now()
                RETURNING project_id
                """,
                payload.project_id,
                payload.grant_id,
                payload.default_base_branch,
                json.dumps(default_policy),
            )
            if row is None:
                raise ValueError(
                    "Connection requires an active same-project repository grant"
                )
            connection = await _fetch_active_connection(conn, payload.project_id)
    if connection is None:  # pragma: no cover - protected by one transaction
        raise RuntimeError("Repository grant became inactive while binding connection")
    return connection


async def activate_operator_grant(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    installation_id: int,
    repository_id: int,
    repository_full_name: str,
    default_base_branch: str,
    authorization_subject: str,
) -> Connection:
    """Atomically authorize and bind a repository verified by an operator.

    This store primitive is intended for a local operator CLI, never a tenant
    route.  A prior active grant is revoked but retained for audit; the existing
    tenant policy is preserved across the reauthorization.
    """
    grant_id = f"ghg_{uuid.uuid4().hex}"
    default_policy = TenantCodegenConnectionPolicy().model_dump(mode="json")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE github_repository_grants
                SET status = 'revoked', revoked_at = now(), updated_at = now()
                WHERE project_id = $1 AND status = 'active'
                """,
                project_id,
            )
            await conn.fetchrow(
                """
                INSERT INTO github_repository_grants
                    (grant_id, project_id, installation_id, repository_id,
                     repository_full_name, status, authorization_source,
                     authorization_subject, verified_at)
                VALUES ($1, $2, $3, $4, $5, 'active', 'operator', $6, now())
                RETURNING *
                """,
                grant_id,
                project_id,
                installation_id,
                repository_id,
                repository_full_name,
                authorization_subject,
            )
            await conn.fetchrow(
                """
                INSERT INTO codegen_connections
                    (project_id, grant_id, default_base_branch, tenant_policy)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (project_id) DO UPDATE SET
                    grant_id = EXCLUDED.grant_id,
                    default_base_branch = EXCLUDED.default_base_branch,
                    updated_at = now()
                RETURNING project_id
                """,
                project_id,
                grant_id,
                default_base_branch,
                json.dumps(default_policy),
            )
            connection = await _fetch_active_connection(conn, project_id)
    if connection is None:  # pragma: no cover - protected by one transaction
        raise RuntimeError("Operator repository grant was not bound")
    return connection


async def get_repository_grant(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    grant_id: str,
) -> RepositoryGrant | None:
    """Return a grant, including revoked audit records."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM github_repository_grants
            WHERE project_id = $1 AND grant_id = $2
            """,
            project_id,
            grant_id,
        )
    return _row_to_repository_grant(row) if row else None


async def get_active_repository_grant(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    grant_id: str,
) -> RepositoryGrant | None:
    """Return a grant only while its repository authority is active."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
            FROM github_repository_grants
            WHERE project_id = $1
              AND grant_id = $2
              AND status = 'active'
              AND verified_at IS NOT NULL
              AND revoked_at IS NULL
            """,
            project_id,
            grant_id,
        )
    return _row_to_repository_grant(row) if row else None


async def revoke_repository_grant(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    grant_id: str,
) -> bool:
    """Irreversibly revoke an active repository grant."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE github_repository_grants
            SET status = 'revoked', revoked_at = now(), updated_at = now()
            WHERE project_id = $1 AND grant_id = $2 AND status = 'active'
            RETURNING grant_id
            """,
            project_id,
            grant_id,
        )
    return row is not None


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
              AND EXISTS (
                  SELECT 1
                  FROM github_repository_grants AS grant_record
                  WHERE grant_record.project_id = codegen_connections.project_id
                    AND grant_record.grant_id = codegen_connections.grant_id
                    AND grant_record.status = 'active'
                    AND grant_record.verified_at IS NOT NULL
                    AND grant_record.revoked_at IS NULL
              )
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
              AND EXISTS (
                  SELECT 1
                  FROM github_repository_grants AS grant_record
                  WHERE grant_record.project_id = codegen_connections.project_id
                    AND grant_record.grant_id = codegen_connections.grant_id
                    AND grant_record.status = 'active'
                    AND grant_record.verified_at IS NOT NULL
                    AND grant_record.revoked_at IS NULL
              )
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
    """Return the project's binding only while its grant remains active."""
    async with pool.acquire() as conn:
        return await _fetch_active_connection(conn, project_id)


async def get_connection_for_changeset(
    pool: asyncpg.Pool, changeset_id: str
) -> Connection | None:
    """Load the active immutable target captured by one changeset.

    This intentionally does not consult ``codegen_connections``.  Rebinding a
    project cannot retarget queued or open work, while revoking its captured
    grant immediately makes this lookup fail closed.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                changeset.project_id,
                changeset.repository_grant_id AS grant_id,
                changeset.repository_installation_id AS installation_id,
                changeset.repository_id,
                changeset.repository_full_name,
                changeset.base_branch AS default_base_branch,
                changeset.tenant_policy_snapshot AS tenant_policy,
                changeset.created_at,
                changeset.updated_at
            FROM codegen_changesets AS changeset
            JOIN github_repository_grants AS grant_record
              ON grant_record.project_id = changeset.project_id
             AND grant_record.grant_id = changeset.repository_grant_id
             AND grant_record.installation_id
                    = changeset.repository_installation_id
             AND grant_record.repository_id = changeset.repository_id
            WHERE changeset.changeset_id = $1
              AND NOT changeset.repository_target_quarantined
              AND changeset.base_branch IS NOT NULL
              AND changeset.tenant_policy_snapshot IS NOT NULL
              AND grant_record.status = 'active'
              AND grant_record.verified_at IS NOT NULL
              AND grant_record.revoked_at IS NULL
            """,
            changeset_id,
        )
    return _row_to_connection(row) if row else None


async def delete_connection(pool: asyncpg.Pool, project_id: str) -> bool:
    """Remove the project binding without deleting its grant audit record."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM codegen_connections WHERE project_id = $1 RETURNING project_id",
            project_id,
        )
    return row is not None
