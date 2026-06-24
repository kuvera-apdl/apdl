"""Persistence for the code-implementation work queue (``feature_proposals``).

Decision D2 (hybrid): human-approved feature proposals are durable rows here.
The ``code_implementation`` agent claims approved rows with ``FOR UPDATE SKIP
LOCKED`` so two concurrent runs never implement the same proposal twice. The
approval endpoint enqueues + opportunistically kicks a run; this table is the
single source of truth for what still needs building.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

FEATURE_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS feature_proposals (
    proposal_id   TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    run_id        TEXT,
    status        TEXT NOT NULL DEFAULT 'approved',
    title         TEXT NOT NULL,
    spec          TEXT NOT NULL,
    priority      TEXT,
    changeset_id  TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _spec_of(proposal: dict[str, Any]) -> str:
    """Build a codegen spec from an LLM proposal.

    Real proposals carry ``proposed_solution`` / ``implementation_spec`` /
    ``problem_statement`` rather than ``spec`` / ``description``; fall back
    across all of them (and append the structured ``implementation_spec``) so a
    proposal is never silently dropped for an empty spec — the cause of the
    "claimed 0 proposals" handoff failure.
    """

    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return json.dumps(value, default=str) if value else ""

    prose = ""
    for key in ("proposed_solution", "spec", "description", "problem_statement"):
        prose = _text(proposal.get(key))
        if prose:
            break
    detail = _text(proposal.get("implementation_spec"))
    return "\n\n".join(part for part in (prose, detail) if part)


async def enqueue_proposals(
    pool: asyncpg.Pool, run_id: str, project_id: str, proposals: list[dict[str, Any]]
) -> int:
    """Insert approved proposals as ``approved`` queue rows (idempotent on id)."""
    inserted = 0
    async with pool.acquire() as conn:
        for proposal in proposals:
            proposal_id = str(proposal.get("proposal_id") or "").strip()
            title = str(proposal.get("title") or "").strip()
            spec = _spec_of(proposal)
            if not proposal_id or not title or not spec:
                logger.warning(
                    "Skipping proposal with missing fields (id=%r title=%r spec_empty=%s)",
                    proposal_id,
                    title,
                    not spec,
                )
                continue
            await conn.execute(
                """
                INSERT INTO feature_proposals
                    (proposal_id, project_id, run_id, status, title, spec, priority)
                VALUES ($1, $2, $3, 'approved', $4, $5, $6)
                ON CONFLICT (proposal_id) DO NOTHING
                """,
                proposal_id,
                project_id,
                run_id,
                title,
                spec,
                str(proposal.get("priority") or ""),
            )
            inserted += 1
    return inserted


async def claim_proposals(
    pool: asyncpg.Pool,
    project_id: str,
    limit: int = 5,
    proposal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Claim up to ``limit`` approved proposals, marking them ``implementing``.

    ``FOR UPDATE SKIP LOCKED`` makes concurrent claims disjoint, so the drain is
    safe to run from both the approval kick and a scheduled sweep. When
    ``proposal_id`` is given, claim only that proposal (one PR per approved
    proposal); the ``status='approved'`` guard still applies, so an
    already-claimed proposal yields an empty (no-op) claim rather than a
    duplicate build.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            if proposal_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT proposal_id, title, spec, priority
                    FROM feature_proposals
                    WHERE project_id = $1 AND status = 'approved' AND proposal_id = $2
                    FOR UPDATE SKIP LOCKED
                    """,
                    project_id,
                    proposal_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT proposal_id, title, spec, priority
                    FROM feature_proposals
                    WHERE project_id = $1 AND status = 'approved'
                    ORDER BY created_at
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                    """,
                    project_id,
                    limit,
                )
            claimed = [dict(r) for r in rows]
            if claimed:
                await conn.execute(
                    """
                    UPDATE feature_proposals
                    SET status = 'implementing', updated_at = now()
                    WHERE proposal_id = ANY($1::text[])
                    """,
                    [c["proposal_id"] for c in claimed],
                )
    return claimed


async def mark_implemented(pool: asyncpg.Pool, proposal_id: str, changeset_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE feature_proposals
            SET status = 'implemented', changeset_id = $2, updated_at = now()
            WHERE proposal_id = $1
            """,
            proposal_id,
            changeset_id,
        )


async def mark_failed(pool: asyncpg.Pool, proposal_id: str, error: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE feature_proposals
            SET status = 'failed', error = $2, updated_at = now()
            WHERE proposal_id = $1
            """,
            proposal_id,
            error,
        )
