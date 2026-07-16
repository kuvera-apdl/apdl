"""DB-backed, short-lived GitHub installation-token leases.

Callers identify an APDL project or changeset, never a GitHub installation or
repository.  The broker resolves immutable repository authority from an active
grant immediately before minting, applies one fixed least-privilege profile,
and revokes the credential when the operation leaves its context manager.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Protocol

import asyncpg

from app.github.app_auth import (
    CODEGEN_PR_WRITE_PERMISSIONS,
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

_GRANT_REVOCATION_CHANNEL = "codegen_repository_grant_revoked"
_REVOKE_TIMEOUT_SECONDS = 10
_WRITE_TOKEN_MINIMUM_TTL = timedelta(minutes=5)


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
        self._active_leases: dict[str, dict[str, str]] = {}
        self._active_lock = asyncio.Lock()
        self._listener_connection: asyncpg.Connection | None = None
        self._notification_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Listen for transactional grant revocations from every replica."""
        if self._listener_connection is not None:
            return
        connection = await self._pool.acquire()
        try:
            await connection.add_listener(
                _GRANT_REVOCATION_CHANNEL,
                self._on_grant_revoked,
            )
        except BaseException:
            await self._pool.release(connection)
            raise
        self._listener_connection = connection

    async def close(self) -> None:
        """Stop listening and revoke every credential still owned locally."""
        connection = self._listener_connection
        self._listener_connection = None
        if connection is not None:
            try:
                await connection.remove_listener(
                    _GRANT_REVOCATION_CHANNEL,
                    self._on_grant_revoked,
                )
            finally:
                await self._pool.release(connection)

        async with self._active_lock:
            tokens = {
                token
                for leases in self._active_leases.values()
                for token in leases.values()
            }
            self._active_leases.clear()
        if tokens:
            await asyncio.gather(
                *(self._revoke_with_cancellation_barrier(token) for token in tokens)
            )
        if self._notification_tasks:
            await asyncio.gather(
                *tuple(self._notification_tasks),
                return_exceptions=True,
            )

    def _on_grant_revoked(
        self,
        _connection: asyncpg.Connection,
        _process_id: int,
        _channel: str,
        grant_id: str,
    ) -> None:
        """Schedule immediate local cleanup for one committed revocation."""
        if not grant_id:
            logger.error("Received an empty repository-grant revocation payload")
            return
        task = asyncio.create_task(self._revoke_active_grant(grant_id))
        self._notification_tasks.add(task)
        task.add_done_callback(self._notification_task_finished)

    def _notification_task_finished(self, task: asyncio.Task[None]) -> None:
        self._notification_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Repository-grant revocation cleanup failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _register_active_token(self, grant_id: str, token: str) -> str:
        lease_id = uuid.uuid4().hex
        async with self._active_lock:
            self._active_leases.setdefault(grant_id, {})[lease_id] = token
        return lease_id

    async def _claim_active_token(
        self,
        grant_id: str,
        lease_id: str,
    ) -> str | None:
        async with self._active_lock:
            leases = self._active_leases.get(grant_id)
            if leases is None:
                return None
            token = leases.pop(lease_id, None)
            if not leases:
                self._active_leases.pop(grant_id, None)
            return token

    async def _revoke_active_grant(self, grant_id: str) -> None:
        async with self._active_lock:
            leases = self._active_leases.pop(grant_id, {})
        tokens = set(leases.values())
        if tokens:
            await asyncio.gather(*(self._revoke_safely(token) for token in tokens))

    async def _revoke_safely(self, token: str) -> None:
        try:
            await asyncio.wait_for(
                self._revoke_token(token),
                timeout=_REVOKE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error("Timed out revoking leased GitHub installation token")
        except Exception:
            # Revocation is defense in depth for an already repository- and
            # permission-scoped credential. A transient GitHub cleanup
            # failure must not erase the authoritative outcome of a push or
            # PR that GitHub already accepted; the token still expires at
            # GitHub's issued expiry.
            logger.exception("Could not revoke leased GitHub installation token")

    async def _revoke_with_cancellation_barrier(self, token: str) -> None:
        cleanup = asyncio.create_task(self._revoke_safely(token))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # Finish the bounded HTTP cleanup before propagating cancellation.
            await cleanup
            raise

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
        lease_id: str | None = None
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
            lease_id = await self._register_active_token(
                target.grant_id,
                issued.token,
            )
            # Close the notification-before-registration race. If authority
            # changed after the first check, the notification owns revocation;
            # this second read prevents the token from ever reaching a caller.
            current = await revalidate()
            if current.target != target:
                raise RepositoryAuthorizationError(
                    "Repository grant changed while registering the GitHub token"
                )
            yield issued.token
        finally:
            token = (
                await self._claim_active_token(target.grant_id, lease_id)
                if lease_id is not None
                else issued.token
            )
            if token is not None:
                await self._revoke_with_cancellation_barrier(token)

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
        """Lease a contents-write token for one changeset's branch mutation."""
        connection = await self._changeset_connection(changeset_id)
        async with self._lease(
            connection,
            CODEGEN_WRITE_PERMISSIONS,
            lambda: self._changeset_connection(changeset_id),
            minimum_ttl=_WRITE_TOKEN_MINIMUM_TTL,
        ) as token:
            yield token

    @asynccontextmanager
    async def pr_write_changeset(self, changeset_id: str) -> AsyncIterator[str]:
        """Lease PR-write authority without repository contents mutation."""
        connection = await self._changeset_connection(changeset_id)
        async with self._lease(
            connection,
            CODEGEN_PR_WRITE_PERMISSIONS,
            lambda: self._changeset_connection(changeset_id),
            minimum_ttl=_WRITE_TOKEN_MINIMUM_TTL,
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
