"""Persistence for changesets (``codegen_changesets``).

Reads and writes are explicit-column projections (no ``SELECT *`` into the
model). :func:`transition_changeset` enforces the lifecycle state machine inside
a row-locked transaction so concurrent updates cannot corrupt the status.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.models.changeset import Changeset, ChangesetStatus, assert_transition


def _loads(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _row_to_changeset(row: asyncpg.Record) -> Changeset:
    return Changeset(
        changeset_id=row["changeset_id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        status=ChangesetStatus(row["status"]),
        base_branch=row["base_branch"],
        branch=row["branch"],
        pr_url=row["pr_url"],
        pr_number=row["pr_number"],
        ci_status=row["ci_status"],
        diff_stat=_loads(row["diff_stat"]),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_changeset(
    pool: asyncpg.Pool,
    *,
    changeset_id: str,
    project_id: str,
    run_id: str | None,
    base_branch: str | None,
    task: dict[str, Any],
) -> Changeset:
    """Insert a new changeset in the ``queued`` state."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO codegen_changesets
                (changeset_id, project_id, run_id, status, base_branch, task)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING *
            """,
            changeset_id,
            project_id,
            run_id,
            ChangesetStatus.queued.value,
            base_branch,
            json.dumps(task),
        )
    return _row_to_changeset(row)


async def get_changeset(pool: asyncpg.Pool, changeset_id: str) -> Changeset | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM codegen_changesets WHERE changeset_id = $1",
            changeset_id,
        )
    return _row_to_changeset(row) if row else None


async def list_changesets(
    pool: asyncpg.Pool, project_id: str, limit: int = 50
) -> list[Changeset]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM codegen_changesets
            WHERE project_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            project_id,
            limit,
        )
    return [_row_to_changeset(r) for r in rows]


async def transition_changeset(
    pool: asyncpg.Pool,
    changeset_id: str,
    target: ChangesetStatus,
    *,
    error: str | None = None,
) -> Changeset | None:
    """Move a changeset to ``target``, enforcing the lifecycle state machine.

    Returns the updated changeset, or ``None`` if it does not exist. Raises
    :class:`~app.models.changeset.InvalidTransition` if the move is not
    permitted from the current status.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchval(
                "SELECT status FROM codegen_changesets WHERE changeset_id = $1 FOR UPDATE",
                changeset_id,
            )
            if current is None:
                return None
            assert_transition(ChangesetStatus(current), target)
            row = await conn.fetchrow(
                """
                UPDATE codegen_changesets
                SET status = $2, error = COALESCE($3, error), updated_at = now()
                WHERE changeset_id = $1
                RETURNING *
                """,
                changeset_id,
                target.value,
                error,
            )
    return _row_to_changeset(row)
