"""Persistence for changesets (``codegen_changesets``).

Reads and writes are explicit-column projections (no ``SELECT *`` into the
model). :func:`transition_changeset` enforces the lifecycle state machine inside
a row-locked transaction so concurrent updates cannot corrupt the status.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.models.changeset import (
    CI_SYNCABLE_STATUSES,
    Changeset,
    ChangesetStatus,
    TaskSpec,
    assert_transition,
)
from app.store.jsonb import loads_jsonb

#: Pre-PR pipeline states a running job actively drives. The job runner uses
#: in-process background tasks, so a process restart orphans any changeset
#: sitting here — :func:`fail_stale_changesets` sweeps the stale ones.
_TRANSIENT_STATUSES: tuple[ChangesetStatus, ...] = (
    ChangesetStatus.queued,
    ChangesetStatus.cloning,
    ChangesetStatus.editing,
    ChangesetStatus.testing,
    ChangesetStatus.pushing,
)


def _row_to_changeset(row: asyncpg.Record) -> Changeset:
    return Changeset(
        changeset_id=row["changeset_id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        task=TaskSpec(**loads_jsonb(row["task"])),
        status=ChangesetStatus(row["status"]),
        base_branch=row["base_branch"],
        branch=row["branch"],
        pr_url=row["pr_url"],
        pr_number=row["pr_number"],
        pr_node_id=row["pr_node_id"],
        ci_status=row["ci_status"],
        diff_stat=loads_jsonb(row["diff_stat"]),
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


async def get_changeset_by_branch(
    pool: asyncpg.Pool, branch: str, repo: str
) -> Changeset | None:
    """Find the active changeset for a ``branch`` on a specific ``repo``.

    Used to route GitHub webhooks. Scoped by repo (joined through the project's
    connection) as well as branch + status, so two connected repos that happen
    to share a branch name can't mis-route each other's CI events.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT cs.* FROM codegen_changesets cs
            JOIN codegen_connections conn ON conn.project_id = cs.project_id
            WHERE cs.branch = $1
              AND conn.repo = $2
              AND cs.status IN ('pr_open', 'ci_running', 'ci_failed', 'ci_passed')
            ORDER BY cs.created_at DESC
            LIMIT 1
            """,
            branch,
            repo,
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


async def list_syncable_changeset_ids(pool: asyncpg.Pool) -> list[str]:
    """Ids of changesets whose CI status a poll can still advance, oldest first.

    The CI poller sweeps these every interval. ``sync_ci_status`` re-checks each
    one under a row lock, so an id that has moved to a terminal/ineligible state
    by the time it runs is simply a no-op.
    """
    statuses = [s.value for s in CI_SYNCABLE_STATUSES]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT changeset_id FROM codegen_changesets
            WHERE status = ANY($1::text[])
            ORDER BY updated_at ASC
            """,
            statuses,
        )
    return [r["changeset_id"] for r in rows]


async def _guarded_update(
    pool: asyncpg.Pool,
    changeset_id: str,
    target: ChangesetStatus,
    *,
    set_clause: str,
    params: tuple[Any, ...],
) -> Changeset | None:
    """Row-locked, state-machine-checked status update.

    The single place the ``SELECT … FOR UPDATE`` → :func:`assert_transition` →
    ``UPDATE … RETURNING`` dance lives, so concurrent updates can't corrupt the
    status. ``set_clause`` adds columns beyond ``status``/``updated_at`` and is
    composed only from trusted in-module SQL literals (never request data); its
    bind values are ``$3``+ supplied in ``params``. Returns the updated
    changeset, ``None`` if it does not exist, or raises
    :class:`~app.models.changeset.InvalidTransition` for an illegal move.
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
                f"""
                UPDATE codegen_changesets
                SET status = $2, {set_clause}, updated_at = now()
                WHERE changeset_id = $1
                RETURNING *
                """,
                changeset_id,
                target.value,
                *params,
            )
    return _row_to_changeset(row)


async def transition_changeset(
    pool: asyncpg.Pool,
    changeset_id: str,
    target: ChangesetStatus,
    *,
    error: str | None = None,
) -> Changeset | None:
    """Move a changeset to ``target``, enforcing the lifecycle state machine."""
    return await _guarded_update(
        pool,
        changeset_id,
        target,
        set_clause="error = COALESCE($3, error)",
        params=(error,),
    )


async def mark_pr_open(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    branch: str,
    pr_url: str,
    pr_number: int,
    diff_stat: dict[str, Any],
    node_id: str = "",
) -> Changeset | None:
    """Transition ``pushing → pr_open`` and persist the branch + PR identifiers."""
    return await _guarded_update(
        pool,
        changeset_id,
        ChangesetStatus.pr_open,
        set_clause=(
            "branch = $3, pr_url = $4, pr_number = $5, "
            "pr_node_id = $6, diff_stat = $7::jsonb"
        ),
        params=(branch, pr_url, pr_number, node_id, json.dumps(diff_stat)),
    )


async def set_ci_status(
    pool: asyncpg.Pool,
    changeset_id: str,
    *,
    target: ChangesetStatus,
    ci_status: str,
) -> Changeset | None:
    """Transition to ``target`` and persist the external ``ci_status`` string.

    Used to move ``pr_open → ci_running → ci_passed | ci_failed`` as the repo's
    own CI reports in (via webhook or poll).
    """
    return await _guarded_update(
        pool,
        changeset_id,
        target,
        set_clause="ci_status = $3",
        params=(ci_status,),
    )


async def fail_stale_changesets(
    pool: asyncpg.Pool, *, older_than_seconds: int, error: str
) -> list[str]:
    """Fail changesets orphaned mid-pipeline past a deadline; return their ids.

    The job runner uses in-process background tasks, so a process restart leaves
    any changeset in a transient (pre-PR) state stuck there forever — no later
    step ever runs to advance or fail it. This sweep (run once at startup) moves
    those rows to ``error`` so they surface instead of hanging. The
    ``older_than_seconds`` deadline guards against killing work a *concurrent*
    codegen replica may still be running on the shared database: set it longer
    than any single job can take (e.g. ``2 × CODEGEN_TIMEOUT``).
    """
    statuses = [s.value for s in _TRANSIENT_STATUSES]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE codegen_changesets
            SET status = 'error', error = COALESCE(error, $1), updated_at = now()
            WHERE status = ANY($2::text[])
              AND updated_at < now() - $3 * interval '1 second'
            RETURNING changeset_id
            """,
            error,
            statuses,
            older_than_seconds,
        )
    return [r["changeset_id"] for r in rows]
