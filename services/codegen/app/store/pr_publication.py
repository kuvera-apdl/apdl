"""Append-only persistence for recoverable GitHub PR publication."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import asyncpg
from pydantic import BaseModel

from app.models.changeset import ChangesetStatus
from app.models.pr_publication import (
    PULL_REQUEST_PUBLICATION_EVENT_ADAPTER,
    PublicationBranchPublished,
    PublicationCleanupConfirmed,
    PublicationCreateAccepted,
    PublicationIntentRecorded,
    PublicationManualIntervention,
    PullRequestPublicationEvent,
)
from app.store.jsonb import loads_jsonb


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ResultT = TypeVar("_ResultT")
_PUBLICATION_LOCK_NAMESPACE = b"apdl:codegen-pr-publication:v1\0"


class _BorrowedConnectionAcquire:
    """No-op acquire context for store calls already holding one connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class PublicationConnectionPool:
    """Pool-shaped adapter that reuses the advisory-lock-owning connection."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _BorrowedConnectionAcquire:
        return _BorrowedConnectionAcquire(self._conn)


def _publication_lock_key(changeset_id: str) -> int:
    digest = hashlib.sha256(
        _PUBLICATION_LOCK_NAMESPACE + changeset_id.encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _finish_despite_cancellation(
    awaitable: Awaitable[_ResultT],
) -> tuple[_ResultT, asyncio.CancelledError | None]:
    task = asyncio.create_task(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    return task.result(), cancellation


def _terminate_connection(conn: asyncpg.Connection) -> None:
    terminate = getattr(conn, "terminate", None)
    if callable(terminate):
        terminate()


async def _release_publication_lock(
    conn: asyncpg.Connection,
    lock_key: int,
) -> None:
    try:
        released, cancellation = await _finish_despite_cancellation(
            conn.fetchval(
                "SELECT pg_advisory_unlock($1::bigint)",
                lock_key,
            )
        )
    except BaseException:
        _terminate_connection(conn)
        raise
    if released is not True:
        _terminate_connection(conn)
        raise RuntimeError("PostgreSQL did not release the exact publication lock")
    if cancellation is not None:
        raise cancellation


@asynccontextmanager
async def acquire_publication_lock(
    pool: asyncpg.Pool,
    changeset_id: str,
) -> AsyncIterator[PublicationConnectionPool | None]:
    """Try-lock one publication and reuse its session for every store call."""
    lock_key = _publication_lock_key(changeset_id)
    async with pool.acquire() as conn:
        try:
            acquired, cancellation = await _finish_despite_cancellation(
                conn.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)",
                    lock_key,
                )
            )
        except BaseException:
            _terminate_connection(conn)
            raise
        if cancellation is not None:
            if acquired is True:
                await _release_publication_lock(conn, lock_key)
            raise cancellation
        if acquired is False:
            yield None
            return
        if acquired is not True:
            _terminate_connection(conn)
            raise RuntimeError("PostgreSQL returned invalid publication lock state")
        try:
            yield PublicationConnectionPool(conn)
        finally:
            await _release_publication_lock(conn, lock_key)


def _payload(value: Any) -> dict[str, Any]:
    parsed = loads_jsonb(value)
    if not isinstance(parsed, dict):
        raise ValueError("publication event payload must be an object")
    return parsed


def _validate_model(model: type[_ModelT], value: Any) -> _ModelT:
    if isinstance(value, str):
        return model.model_validate_json(value)
    return model.model_validate(value)


def _validate_event(value: Any) -> PullRequestPublicationEvent:
    if isinstance(value, str):
        return PULL_REQUEST_PUBLICATION_EVENT_ADAPTER.validate_json(value)
    return PULL_REQUEST_PUBLICATION_EVENT_ADAPTER.validate_python(value)


async def _append_event(
    conn: asyncpg.Connection,
    event: PullRequestPublicationEvent,
) -> bool:
    validated = PULL_REQUEST_PUBLICATION_EVENT_ADAPTER.validate_python(
        event.model_dump(mode="python")
    )
    intent_event_id = getattr(validated, "intent_event_id", None)
    cleanup_request_event_id = getattr(validated, "cleanup_request_event_id", None)
    pr_number: int | None = getattr(validated, "pr_number", None)
    github_url: str | None = getattr(validated, "github_url", None)
    if isinstance(validated, PublicationCreateAccepted):
        pr_number = validated.receipt.pr_number
        github_url = validated.receipt.github_url
    inserted = await conn.fetchval(
        """
        INSERT INTO codegen_pull_request_publication_events
            (event_id, changeset_id, event_type, intent_event_id,
             cleanup_request_event_id, pr_number, github_url,
             recorded_at, payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
        """,
        validated.event_id,
        validated.changeset_id,
        validated.event_type,
        intent_event_id,
        cleanup_request_event_id,
        pr_number,
        github_url,
        validated.recorded_at,
        validated.model_dump_json(),
    )
    return inserted is not None


async def record_intent(
    pool: asyncpg.Pool,
    intent: PublicationIntentRecorded,
) -> PublicationIntentRecorded:
    """Persist one immutable intent before any branch or PR mutation."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT payload
                FROM codegen_pull_request_publication_events
                WHERE changeset_id = $1 AND event_type = 'intent_recorded'
                """,
                intent.changeset_id,
            )
            if existing is not None:
                persisted = _validate_model(
                    PublicationIntentRecorded, existing["payload"]
                )
                expected = intent.model_dump(
                    mode="json", exclude={"event_id", "recorded_at"}
                )
                observed = persisted.model_dump(
                    mode="json", exclude={"event_id", "recorded_at"}
                )
                if observed != expected:
                    raise ValueError(
                        "changeset already has a different publication intent"
                    )
                return persisted
            current = await conn.fetchval(
                """
                SELECT status FROM codegen_changesets
                WHERE changeset_id = $1
                FOR UPDATE
                """,
                intent.changeset_id,
            )
            if current is None:
                raise ValueError("publication intent references an unknown changeset")
            if ChangesetStatus(current) is not ChangesetStatus.pushing:
                raise ValueError("publication intent requires pushing status")
            if not await _append_event(conn, intent):
                raise ValueError("publication intent event already exists")
            await conn.execute(
                """
                UPDATE codegen_changesets
                SET branch = $2, updated_at = now()
                WHERE changeset_id = $1
                """,
                intent.changeset_id,
                intent.branch,
            )
    return intent


async def get_intent(
    pool: asyncpg.Pool,
    changeset_id: str,
) -> PublicationIntentRecorded | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT payload
            FROM codegen_pull_request_publication_events
            WHERE changeset_id = $1 AND event_type = 'intent_recorded'
            """,
            changeset_id,
        )
    if row is None:
        return None
    return _validate_model(PublicationIntentRecorded, row["payload"])


async def get_published_branch(
    pool: asyncpg.Pool,
    changeset_id: str,
) -> PublicationBranchPublished | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT payload
            FROM codegen_pull_request_publication_events
            WHERE changeset_id = $1 AND event_type = 'branch_published'
            ORDER BY event_sequence DESC
            LIMIT 1
            """,
            changeset_id,
        )
    if row is None:
        return None
    return _validate_model(PublicationBranchPublished, row["payload"])


async def append_event(
    pool: asyncpg.Pool,
    event: PullRequestPublicationEvent,
) -> bool:
    """Append one strict event and retain typed accepted identity on changeset."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await _append_event(conn, event)
            if inserted and isinstance(event, PublicationBranchPublished):
                await conn.execute(
                    """
                    UPDATE codegen_changesets
                    SET branch = $2, head_sha = $3, updated_at = now()
                    WHERE changeset_id = $1 AND status = 'pushing'
                    """,
                    event.changeset_id,
                    event.branch,
                    event.head_sha,
                )
            elif inserted and isinstance(event, PublicationCreateAccepted):
                await conn.execute(
                    """
                    UPDATE codegen_changesets
                    SET pr_number = COALESCE($2, pr_number),
                        pr_url = COALESCE($3, pr_url),
                        updated_at = now()
                    WHERE changeset_id = $1 AND status = 'pushing'
                    """,
                    event.changeset_id,
                    event.receipt.pr_number,
                    event.receipt.github_url,
                )
    return inserted


async def append_terminal_event_and_error(
    pool: asyncpg.Pool,
    event: PublicationCleanupConfirmed | PublicationManualIntervention,
    *,
    error: str,
) -> None:
    """Atomically journal terminal publication state and project ``error``."""
    if (
        isinstance(event, PublicationCleanupConfirmed)
        and event.next_action != "terminal_error"
    ):
        raise ValueError("only terminal cleanup confirmation may project error")
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                """
                SELECT status FROM codegen_changesets
                WHERE changeset_id = $1
                FOR UPDATE
                """,
                event.changeset_id,
            )
            if current is None:
                raise ValueError("terminal publication event references unknown work")
            if ChangesetStatus(current) is ChangesetStatus.error:
                return
            if ChangesetStatus(current) is not ChangesetStatus.pushing:
                raise ValueError(
                    "terminal publication event requires pushing or error status"
                )
            if not await _append_event(conn, event):
                raise ValueError("terminal publication event already exists")
            await conn.execute(
                """
                UPDATE codegen_changesets
                SET status = 'error', error = $2, updated_at = now()
                WHERE changeset_id = $1
                """,
                event.changeset_id,
                error,
            )


async def ensure_terminal_error(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    error: str,
) -> None:
    """Repair a legacy/split terminal event projection without new mutation."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE codegen_changesets
            SET status = 'error', error = COALESCE(error, $2), updated_at = now()
            WHERE changeset_id = $1 AND status = 'pushing'
            """,
            changeset_id,
            error,
        )


async def list_events(
    pool: asyncpg.Pool,
    changeset_id: str,
) -> list[PullRequestPublicationEvent]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT payload
            FROM codegen_pull_request_publication_events
            WHERE changeset_id = $1
            ORDER BY event_sequence ASC
            """,
            changeset_id,
        )
    return [_validate_event(row["payload"]) for row in rows]


async def list_recoverable_ids(
    pool: asyncpg.Pool,
    *,
    older_than_seconds: int,
) -> list[str]:
    """Stale pushing rows with intent are resumable and must never be failed."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT changeset.changeset_id
            FROM codegen_changesets AS changeset
            WHERE changeset.status = 'pushing'
              AND changeset.updated_at < now() - $1 * interval '1 second'
              AND EXISTS (
                  SELECT 1
                  FROM codegen_pull_request_publication_events AS event
                  WHERE event.changeset_id = changeset.changeset_id
                    AND event.event_type = 'intent_recorded'
              )
            ORDER BY changeset.updated_at ASC, changeset.changeset_id ASC
            """,
            older_than_seconds,
        )
    return [row["changeset_id"] for row in rows]
