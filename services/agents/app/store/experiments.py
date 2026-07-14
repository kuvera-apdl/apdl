"""Persistence for designed experiments — the dedup ledger for experiment_design.

Insights barely change between runs, so without a durable record every run
re-designs experiments for the same themes (the same failure mode the
feature_proposals queue solved for proposals). A row is written for every
design the agent produces — whatever its gate outcome — and the ledger feeds
two dedup layers on the next run: a hard skip of insights whose fingerprint
already has a design, and a "previously designed" prompt section covering
rewordings the fingerprint cannot catch.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

DESIGNED_EXPERIMENTS_DDL = """
CREATE TABLE IF NOT EXISTS designed_experiments (
    project_id     TEXT NOT NULL,
    experiment_id  TEXT NOT NULL,
    run_id         TEXT,
    insight_key    TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    hypothesis     TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'designed',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, experiment_id)
);
"""

DESIGNED_EXPERIMENTS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS designed_experiments_project_created_idx
    ON designed_experiments (project_id, created_at DESC);
"""

#: Phase 2: the treatment changeset implementing a deployed design, when one
#: was opened. Idempotent for pre-phase-2 databases.
DESIGNED_EXPERIMENTS_MIGRATE_DDL = """
ALTER TABLE designed_experiments ADD COLUMN IF NOT EXISTS changeset_id TEXT;
"""


def insight_key(value: Any) -> str:
    """Fingerprint an insight (or its title) for exact-match dedup.

    Whitespace-normalized lowercase title — deliberately simple: rewordings
    are the prompt layer's job, this key only has to catch the common case of
    behavior_analysis re-emitting the same insight verbatim between runs.
    """
    if isinstance(value, dict):
        value = value.get("title") or ""
    return " ".join(str(value).lower().split())


async def record_designed_experiment(
    pool: asyncpg.Pool,
    project_id: str,
    run_id: str,
    design: dict[str, Any],
    status: str,
) -> None:
    """Upsert one produced design. ``status`` is the gate outcome
    (deployed / awaiting_approval / halted)."""
    experiment_id = str(design.get("experiment_id") or "").strip()
    if not experiment_id:
        flag = design.get("flag_config")
        experiment_id = str(flag.get("key") or "").strip() if isinstance(flag, dict) else ""
    if not experiment_id:
        logger.warning("Skipping designed-experiment record with no experiment_id")
        return
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO designed_experiments
                (project_id, experiment_id, run_id, insight_key, title, hypothesis, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (project_id, experiment_id)
            DO UPDATE SET status = EXCLUDED.status, run_id = EXCLUDED.run_id,
                          updated_at = now()
            """,
            project_id,
            experiment_id,
            run_id,
            insight_key(design.get("source_insight") or ""),
            str(design.get("flag_config", {}).get("name") if isinstance(design.get("flag_config"), dict) else "")
            or experiment_id,
            str(design.get("hypothesis") or ""),
            status,
        )


async def link_changeset(
    pool: asyncpg.Pool, project_id: str, experiment_id: str, changeset_id: str
) -> None:
    """Attach the treatment changeset to a designed experiment."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE designed_experiments
            SET changeset_id = $3, updated_at = now()
            WHERE project_id = $1 AND experiment_id = $2
            """,
            project_id,
            experiment_id,
            changeset_id,
        )


async def set_designed_experiment_status(
    pool: asyncpg.Pool, project_id: str, experiment_id: str, status: str
) -> None:
    """Update a ledger row's status (e.g. 'iterate_requested' releases the
    source insight for a redesign)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE designed_experiments
            SET status = $3, updated_at = now()
            WHERE project_id = $1 AND experiment_id = $2
            """,
            project_id,
            experiment_id,
            status,
        )


async def get_designed_experiment(
    pool: asyncpg.Pool, project_id: str, experiment_id: str
) -> dict[str, Any] | None:
    """One ledger row (incl. linked changeset), or None."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT experiment_id, insight_key, title, hypothesis, status, changeset_id
            FROM designed_experiments
            WHERE project_id = $1 AND experiment_id = $2
            """,
            project_id,
            experiment_id,
        )
    return dict(row) if row else None


async def list_designed_experiments(
    pool: asyncpg.Pool, project_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    """The project's design ledger, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT experiment_id, insight_key, title, hypothesis, status
            FROM designed_experiments
            WHERE project_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            project_id,
            limit,
        )
    return [dict(r) for r in rows]
