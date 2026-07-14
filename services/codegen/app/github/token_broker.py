"""DB-backed, short-lived GitHub installation-token leases.

Callers identify an APDL project or changeset, never a GitHub installation or
repository.  The broker resolves immutable repository authority from an active
grant immediately before minting, applies one fixed least-privilege profile,
and revokes the credential when the operation leaves its context manager.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Protocol

import asyncpg

from app.config import codegen_job_budget
from app.github.app_auth import (
    CODEGEN_READ_PERMISSIONS,
    CODEGEN_WRITE_PERMISSIONS,
    AuthorizedRepositoryTarget,
    InstallationToken,
    _mint_token_for_repository,
    _revoke_installation_token,
)
from app.models.connection import Connection
from app.store import connections as connection_store

logger = logging.getLogger(__name__)

_WRITE_TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)


class RepositoryAuthorizationError(RuntimeError):
    """The requested project work has no currently active repository grant."""


class _TokenIssuer(Protocol):
    async def __call__(
        self,
        target: AuthorizedRepositoryTarget,
        *,
        permissions: Mapping[str, str],
    ) -> InstallationToken: ...


TokenRevoker = Callable[[str], Awaitable[None]]
ConnectionResolver = Callable[[], Awaitable[Connection]]


class GitHubTokenBroker:
    """Issue and revoke repository credentials through verified DB authority."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        issue_token: _TokenIssuer = _mint_token_for_repository,
        revoke_token: TokenRevoker = _revoke_installation_token,
    ) -> None:
        self._pool = pool
        self._issue_token = issue_token
        self._revoke_token = revoke_token

    @asynccontextmanager
    async def _lease(
        self,
        connection: Connection,
        permissions: Mapping[str, str],
        revalidate: ConnectionResolver,
        *,
        minimum_ttl: timedelta | None = None,
    ) -> AsyncIterator[str]:
        target = connection.target
        issued = await self._issue_token(
            AuthorizedRepositoryTarget(
                installation_id=target.installation_id,
                repository_id=target.repository_id,
            ),
            permissions=permissions,
        )
        try:
            current = await revalidate()
            if current.target != target:
                raise RepositoryAuthorizationError(
                    "Repository grant changed while GitHub minted the token"
                )
            if minimum_ttl is not None:
                expires_at = issued.expires_at
                if expires_at.tzinfo is None or expires_at.utcoffset() is None:
                    raise RepositoryAuthorizationError(
                        "GitHub token expiry is missing timezone authority"
                    )
                remaining = expires_at - datetime.now(timezone.utc)
                if remaining < minimum_ttl:
                    raise RepositoryAuthorizationError(
                        "GitHub write token expires before the configured "
                        "credential-bearing operation deadline"
                    )
            yield issued.token
        finally:
            try:
                await self._revoke_token(issued.token)
            except Exception:
                # Revocation is defense in depth for an already repository- and
                # permission-scoped credential. A transient GitHub cleanup
                # failure must not erase the authoritative outcome of a push or
                # PR that GitHub already accepted; the token still expires at
                # GitHub's issued expiry.
                logger.exception(
                    "Could not revoke leased GitHub installation token"
                )

    async def _changeset_connection(self, changeset_id: str) -> Connection:
        connection = await connection_store.get_connection_for_changeset(
            self._pool, changeset_id
        )
        if connection is None:
            raise RepositoryAuthorizationError(
                f"Changeset '{changeset_id}' has no active repository grant"
            )
        return connection

    @asynccontextmanager
    async def read_changeset(self, changeset_id: str) -> AsyncIterator[str]:
        """Lease a read-only token for one changeset's immutable target."""
        connection = await self._changeset_connection(changeset_id)
        async with self._lease(
            connection,
            CODEGEN_READ_PERMISSIONS,
            lambda: self._changeset_connection(changeset_id),
        ) as token:
            yield token

    @asynccontextmanager
    async def write_changeset(self, changeset_id: str) -> AsyncIterator[str]:
        """Lease a write token for one changeset's immutable target."""
        connection = await self._changeset_connection(changeset_id)
        async with self._lease(
            connection,
            CODEGEN_WRITE_PERMISSIONS,
            lambda: self._changeset_connection(changeset_id),
            minimum_ttl=(
                timedelta(seconds=codegen_job_budget())
                + _WRITE_TOKEN_EXPIRY_MARGIN
            ),
        ) as token:
            yield token

    async def _project_connection(self, project_id: str) -> Connection:
        connection = await connection_store.get_connection(self._pool, project_id)
        if connection is None:
            raise RepositoryAuthorizationError(
                f"Project '{project_id}' has no active repository grant"
            )
        return connection

    @asynccontextmanager
    async def read_project(
        self, project_id: str
    ) -> AsyncIterator[tuple[Connection, str]]:
        """Lease repository read access for one project's active connection."""
        connection = await self._project_connection(project_id)
        async with self._lease(
            connection,
            CODEGEN_READ_PERMISSIONS,
            lambda: self._project_connection(project_id),
        ) as token:
            yield connection, token
