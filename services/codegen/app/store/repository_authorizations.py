"""Persistence for short-lived, project-scoped GitHub user authorization."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.models.connection import Connection, RepositoryTarget
from app.models.repository_authorization import (
    DiscoveredRepository,
    RepositoryAuthorization,
    RepositoryAuthorizationRepository,
    RepositoryAuthorizationStatus,
)
from app.safety.policy import TenantCodegenConnectionPolicy
from app.store.connections import _notify_grant_revoked
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

_EXPIRED_AUTHORIZATION_CLEANUP_LIMIT = 100


async def _purge_expired_authorizations(conn: asyncpg.Connection) -> None:
    """Delete one bounded batch; candidate rows cascade with their flow."""
    await conn.execute(
        """
        WITH expired AS (
            SELECT authorization_id
            FROM github_repository_authorization_flows
            WHERE expires_at <= now()
            ORDER BY expires_at
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        DELETE FROM github_repository_authorization_flows AS flow
        USING expired
        WHERE flow.authorization_id = expired.authorization_id
        """,
        _EXPIRED_AUTHORIZATION_CLEANUP_LIMIT,
    )


async def purge_expired_authorizations(pool: asyncpg.Pool) -> None:
    """Physically remove a bounded batch of expired flows and candidates."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _purge_expired_authorizations(conn)


async def _purge_scoped_expired_authorization(
    conn: asyncpg.Connection,
    *,
    authorization_id: uuid.UUID,
    project_id: str,
    actor_user_id: uuid.UUID,
) -> bool:
    """Delete only the expired flow proven to belong to this project actor."""
    row = await conn.fetchrow(
        """
        DELETE FROM github_repository_authorization_flows
        WHERE authorization_id = $1
          AND project_id = $2
          AND actor_user_id = $3
          AND expires_at <= now()
        RETURNING authorization_id
        """,
        authorization_id,
        project_id,
        actor_user_id,
    )
    return row is not None


class RepositoryAuthorizationError(RuntimeError):
    """Base class for expected authorization-flow state failures."""


class RepositoryAuthorizationNotFound(RepositoryAuthorizationError):
    """The requested flow/candidate is absent or belongs to another actor."""


class RepositoryAuthorizationExpired(RepositoryAuthorizationError):
    """The short-lived flow has expired."""


class RepositoryAuthorizationConflict(RepositoryAuthorizationError):
    """The flow is not in the lifecycle state required by the operation."""


class RepositoryAuthorizationForbidden(RepositoryAuthorizationError):
    """The bound human actor no longer has live project authority."""


@dataclass(frozen=True)
class ClaimedAuthorization:
    """Internal identity recovered only from a one-time state hash."""

    authorization_id: uuid.UUID
    project_id: str
    actor_user_id: uuid.UUID
    expires_at: datetime


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _expired(expires_at: datetime) -> bool:
    return _aware(expires_at) <= datetime.now(timezone.utc)


def _claimed(row: asyncpg.Record | dict[str, Any]) -> ClaimedAuthorization:
    return ClaimedAuthorization(
        authorization_id=uuid.UUID(str(row["authorization_id"])),
        project_id=str(row["project_id"]),
        actor_user_id=uuid.UUID(str(row["actor_user_id"])),
        expires_at=_aware(row["expires_at"]),
    )


def _connection(row: asyncpg.Record | dict[str, Any]) -> Connection:
    target = RepositoryTarget(
        grant_id=row["grant_id"],
        project_id=row["project_id"],
        installation_id=row["installation_id"],
        repository_id=row["repository_id"],
        repository_full_name=row["repository_full_name"],
    )
    result = Connection(
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
    return result.attach_target(target)


async def has_repository_connection_authority(
    pool: asyncpg.Pool,
    *,
    project_id: str,
    actor_user_id: uuid.UUID,
) -> bool:
    """Check live owner or explicitly delegated dual-role authority."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT (
                account.active
                AND (
                    project.owner_user_id = $2
                    OR (
                        'agents:manage' = ANY(
                            COALESCE(membership.roles, ARRAY[]::TEXT[])
                        )
                        AND 'credentials:manage' = ANY(
                            COALESCE(membership.roles, ARRAY[]::TEXT[])
                        )
                    )
                )
            ) AS repository_connection_authorized
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
    return row is not None and bool(row["repository_connection_authorized"])


async def _lock_repository_connection_authority(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    actor_user_id: uuid.UUID,
) -> bool:
    """Recheck and lock all rows that confer completion authority."""
    identity = await conn.fetchrow(
        """
        SELECT project.owner_user_id, account.active
        FROM admin_projects AS project
        JOIN admin_users AS account ON account.user_id = $2
        WHERE project.project_id = $1
        FOR UPDATE OF project
        FOR SHARE OF account
        """,
        project_id,
        actor_user_id,
    )
    if identity is None or not bool(identity["active"]):
        return False
    owner_user_id = identity["owner_user_id"]
    if owner_user_id is not None and uuid.UUID(str(owner_user_id)) == actor_user_id:
        return True
    membership = await conn.fetchrow(
        """
        SELECT roles
        FROM admin_user_projects
        WHERE project_id = $1 AND user_id = $2
        FOR SHARE
        """,
        project_id,
        actor_user_id,
    )
    if membership is None:
        return False
    roles = {str(role) for role in membership["roles"]}
    return {"agents:manage", "credentials:manage"}.issubset(roles)


async def create_authorization(
    pool: asyncpg.Pool,
    *,
    authorization_id: uuid.UUID,
    project_id: str,
    actor_user_id: uuid.UUID,
    state_hash: str,
    expires_at: datetime,
) -> ClaimedAuthorization:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _purge_expired_authorizations(conn)
            row = await conn.fetchrow(
                """
                INSERT INTO github_repository_authorization_flows
                    (authorization_id, project_id, actor_user_id, state_hash,
                     status, expires_at)
                VALUES ($1, $2, $3, $4, 'awaiting_installation', $5)
                RETURNING authorization_id, project_id, actor_user_id, expires_at
                """,
                authorization_id,
                project_id,
                actor_user_id,
                state_hash,
                expires_at,
            )
    if row is None:  # pragma: no cover - INSERT RETURNING invariant
        raise RuntimeError("GitHub repository authorization was not created")
    return _claimed(row)


async def rotate_installation_state(
    pool: asyncpg.Pool,
    *,
    state_hash: str,
    oauth_state_hash: str,
) -> ClaimedAuthorization | None:
    """Atomically consume setup state and install the OAuth state hash."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE github_repository_authorization_flows
            SET state_hash = $2,
                status = 'awaiting_oauth',
                updated_at = now()
            WHERE state_hash = $1
              AND status = 'awaiting_installation'
              AND expires_at > now()
            RETURNING authorization_id, project_id, actor_user_id, expires_at
            """,
            state_hash,
            oauth_state_hash,
        )
    return _claimed(row) if row is not None else None


async def cancel_installation_state(
    pool: asyncpg.Pool,
    *,
    state_hash: str,
) -> ClaimedAuthorization | None:
    """Consume and delete a setup flow that is awaiting organization approval."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM github_repository_authorization_flows
            WHERE state_hash = $1
              AND status = 'awaiting_installation'
              AND expires_at > now()
            RETURNING project_id, actor_user_id, authorization_id, expires_at
            """,
            state_hash,
        )
    return _claimed(row) if row is not None else None


async def consume_oauth_state(
    pool: asyncpg.Pool,
    *,
    state_hash: str,
    consumed_state_hash: str,
) -> ClaimedAuthorization | None:
    """Atomically replace an OAuth state before exchanging the remote code."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE github_repository_authorization_flows
            SET state_hash = $2, updated_at = now()
            WHERE state_hash = $1
              AND status = 'awaiting_oauth'
              AND expires_at > now()
            RETURNING authorization_id, project_id, actor_user_id, expires_at
            """,
            state_hash,
            consumed_state_hash,
        )
    return _claimed(row) if row is not None else None


async def save_discovered_repositories(
    pool: asyncpg.Pool,
    *,
    authorization: ClaimedAuthorization,
    github_user_id: int,
    github_login: str,
    repositories: list[DiscoveredRepository],
) -> None:
    """Persist opaque candidates only after the one-time OAuth state is consumed."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT status, expires_at
                FROM github_repository_authorization_flows
                WHERE authorization_id = $1
                  AND project_id = $2
                  AND actor_user_id = $3
                FOR UPDATE
                """,
                authorization.authorization_id,
                authorization.project_id,
                authorization.actor_user_id,
            )
            if row is None:
                raise RepositoryAuthorizationNotFound
            if _expired(row["expires_at"]):
                raise RepositoryAuthorizationExpired
            if row["status"] != RepositoryAuthorizationStatus.awaiting_oauth.value:
                raise RepositoryAuthorizationConflict

            for repository in repositories:
                await conn.fetchrow(
                    """
                    INSERT INTO github_repository_authorization_candidates
                        (candidate_id, authorization_id, installation_id,
                         repository_id, repository_full_name,
                         default_base_branch, private)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    RETURNING candidate_id
                    """,
                    uuid.uuid4(),
                    authorization.authorization_id,
                    repository.installation_id,
                    repository.repository_id,
                    repository.repository_full_name,
                    repository.default_base_branch,
                    repository.private,
                )
            updated = await conn.fetchrow(
                """
                UPDATE github_repository_authorization_flows
                SET status = 'awaiting_selection',
                    github_user_id = $2,
                    github_login = $3,
                    updated_at = now()
                WHERE authorization_id = $1
                  AND status = 'awaiting_oauth'
                RETURNING authorization_id
                """,
                authorization.authorization_id,
                github_user_id,
                github_login,
            )
            if updated is None:  # pragma: no cover - row lock protects transition
                raise RepositoryAuthorizationConflict


async def get_authorization(
    pool: asyncpg.Pool,
    *,
    authorization_id: uuid.UUID,
    project_id: str,
    actor_user_id: uuid.UUID,
) -> RepositoryAuthorization | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT authorization_id, project_id, status, expires_at
            FROM github_repository_authorization_flows
            WHERE authorization_id = $1
              AND project_id = $2
              AND actor_user_id = $3
            """,
            authorization_id,
            project_id,
            actor_user_id,
        )
        if row is None:
            return None
        repository_rows = []
        if _expired(row["expires_at"]):
            await _purge_scoped_expired_authorization(
                conn,
                authorization_id=authorization_id,
                project_id=project_id,
                actor_user_id=actor_user_id,
            )
        elif row["status"] == RepositoryAuthorizationStatus.awaiting_selection.value:
            repository_rows = await conn.fetch(
                """
                SELECT candidate_id, repository_id, repository_full_name,
                       default_base_branch, private
                FROM github_repository_authorization_candidates
                WHERE authorization_id = $1
                ORDER BY lower(repository_full_name), repository_id
                """,
                authorization_id,
            )
    return RepositoryAuthorization(
        authorization_id=row["authorization_id"],
        project_id=row["project_id"],
        status=row["status"],
        expires_at=_aware(row["expires_at"]),
        repositories=[
            RepositoryAuthorizationRepository.model_validate(dict(item))
            for item in repository_rows
        ],
    )


async def get_completion_candidate(
    pool: asyncpg.Pool,
    *,
    authorization_id: uuid.UUID,
    project_id: str,
    actor_user_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> DiscoveredRepository:
    """Load immutable server-discovered coordinates for live GitHub revalidation."""
    expired = False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT flow.status, flow.expires_at,
                   candidate.installation_id, candidate.repository_id,
                   candidate.repository_full_name, candidate.default_base_branch,
                   candidate.private
            FROM github_repository_authorization_flows AS flow
            JOIN github_repository_authorization_candidates AS candidate
              ON candidate.authorization_id = flow.authorization_id
            WHERE flow.authorization_id = $1
              AND flow.project_id = $2
              AND flow.actor_user_id = $3
              AND candidate.candidate_id = $4
            """,
            authorization_id,
            project_id,
            actor_user_id,
            candidate_id,
        )
        if row is not None and _expired(row["expires_at"]):
            expired = True
            await _purge_scoped_expired_authorization(
                conn,
                authorization_id=authorization_id,
                project_id=project_id,
                actor_user_id=actor_user_id,
            )
    if row is None:
        raise RepositoryAuthorizationNotFound
    if expired:
        raise RepositoryAuthorizationExpired
    if row["status"] != RepositoryAuthorizationStatus.awaiting_selection.value:
        raise RepositoryAuthorizationConflict
    return DiscoveredRepository(
        installation_id=row["installation_id"],
        repository_id=row["repository_id"],
        repository_full_name=row["repository_full_name"],
        default_base_branch=row["default_base_branch"],
        private=row["private"],
    )


async def complete_authorization(
    pool: asyncpg.Pool,
    *,
    authorization_id: uuid.UUID,
    project_id: str,
    actor_user_id: uuid.UUID,
    candidate_id: uuid.UUID,
    verified_candidate: DiscoveredRepository,
) -> Connection:
    """Atomically bind exactly the candidate revalidated against live GitHub."""
    if not isinstance(verified_candidate, DiscoveredRepository):
        raise TypeError("verified_candidate must be a DiscoveredRepository")
    grant_id = f"ghg_{uuid.uuid4().hex}"
    default_policy = TenantCodegenConnectionPolicy().model_dump(mode="json")
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not await _lock_repository_connection_authority(
                conn,
                project_id=project_id,
                actor_user_id=actor_user_id,
            ):
                raise RepositoryAuthorizationForbidden
            flow = await conn.fetchrow(
                """
                SELECT status, github_user_id, expires_at
                FROM github_repository_authorization_flows
                WHERE authorization_id = $1
                  AND project_id = $2
                  AND actor_user_id = $3
                FOR UPDATE
                """,
                authorization_id,
                project_id,
                actor_user_id,
            )
            if flow is None:
                raise RepositoryAuthorizationNotFound
            if _expired(flow["expires_at"]):
                raise RepositoryAuthorizationExpired
            if flow["status"] != RepositoryAuthorizationStatus.awaiting_selection.value:
                raise RepositoryAuthorizationConflict

            candidate = await conn.fetchrow(
                """
                SELECT installation_id, repository_id, repository_full_name,
                       default_base_branch, private
                FROM github_repository_authorization_candidates
                WHERE authorization_id = $1 AND candidate_id = $2
                FOR UPDATE
                """,
                authorization_id,
                candidate_id,
            )
            if candidate is None:
                raise RepositoryAuthorizationNotFound
            locked_candidate = DiscoveredRepository(
                installation_id=candidate["installation_id"],
                repository_id=candidate["repository_id"],
                repository_full_name=candidate["repository_full_name"],
                default_base_branch=candidate["default_base_branch"],
                private=candidate["private"],
            )
            if locked_candidate != verified_candidate:
                raise RepositoryAuthorizationConflict

            revoked = await conn.fetch(
                """
                UPDATE github_repository_grants
                SET status = 'revoked', revoked_at = now(), updated_at = now()
                WHERE project_id = $1 AND status = 'active'
                RETURNING grant_id
                """,
                project_id,
            )
            for revoked_row in revoked:
                await _notify_grant_revoked(conn, revoked_row["grant_id"])

            github_user_id = int(flow["github_user_id"])
            await conn.fetchrow(
                """
                INSERT INTO github_repository_grants
                    (grant_id, project_id, installation_id, repository_id,
                     repository_full_name, status, authorization_source,
                     authorization_subject, authorized_by_user_id,
                     github_user_id, verified_at)
                VALUES ($1, $2, $3, $4, $5, 'active', 'github_oauth',
                        $6, $7, $8, now())
                RETURNING grant_id
                """,
                grant_id,
                project_id,
                locked_candidate.installation_id,
                locked_candidate.repository_id,
                locked_candidate.repository_full_name,
                f"github_user:{github_user_id}",
                actor_user_id,
                github_user_id,
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
                locked_candidate.default_base_branch,
                json.dumps(default_policy),
            )
            completed = await conn.fetchrow(
                """
                UPDATE github_repository_authorization_flows
                SET status = 'completed', completed_at = now(), updated_at = now()
                WHERE authorization_id = $1
                  AND status = 'awaiting_selection'
                RETURNING authorization_id
                """,
                authorization_id,
            )
            if completed is None:  # pragma: no cover - flow lock protects transition
                raise RepositoryAuthorizationConflict
            await conn.execute(
                """
                DELETE FROM github_repository_authorization_candidates
                WHERE authorization_id = $1
                """,
                authorization_id,
            )
            connection_row = await conn.fetchrow(_CONNECTION_SELECT, project_id)
    if connection_row is None:  # pragma: no cover - one transaction invariant
        raise RuntimeError("GitHub repository authorization was not bound")
    return _connection(connection_row)
