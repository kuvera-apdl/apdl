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


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, default=str) if value else ""


def _as_bullets(value: Any) -> list[str]:
    """Render a proposal list field as markdown bullet bodies."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [_text(value)] if value else []


def _impl_notes(impl: Any) -> str:
    """Render ``implementation_spec`` as readable markdown, not a JSON blob.

    The raw ``json.dumps`` this replaces reached the coding agent verbatim and
    read as noise at best — and its ``dependencies`` list (often organizational)
    as a mandate to descope the change to nothing. Known fields become labeled
    bullets; unknown fields still surface (serialized) so no detail is dropped.
    """
    if not impl:
        return ""
    if not isinstance(impl, dict):
        return _text(impl)
    labeled = (
        ("components_affected", "Components affected"),
        ("technical_considerations", "Technical considerations"),
        ("dependencies", "In-repo prerequisites"),
    )
    parts: list[str] = []
    for key, label in labeled:
        bullets = _as_bullets(impl.get(key))
        if bullets:
            parts.append(f"**{label}:**\n" + "\n".join(f"- {b}" for b in bullets))
    effort = str(impl.get("estimated_effort") or "").strip()
    if effort:
        parts.append(f"**Estimated effort:** {effort}")
    rest = {
        k: v
        for k, v in impl.items()
        if k not in {key for key, _ in labeled} and k != "estimated_effort" and v
    }
    if rest:
        parts.append(json.dumps(rest, default=str))
    return "\n\n".join(parts)


def _acceptance_criteria(value: Any) -> list[str]:
    """Render ``success_criteria`` entries as checkable bullet bodies."""
    criteria: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            metric = str(item.get("metric") or "").strip()
            target = str(item.get("target") or "").strip()
            timeframe = str(item.get("timeframe") or "").strip()
            text = " — ".join(p for p in (metric, target) if p)
            if text and timeframe:
                text = f"{text} (within {timeframe})"
            if text:
                criteria.append(text)
        elif str(item).strip():
            criteria.append(str(item).strip())
    return criteria


def _spec_of(proposal: dict[str, Any]) -> str:
    """Build a codegen spec from an LLM proposal, rendered as a markdown work order.

    Real proposals carry ``proposed_solution`` / ``implementation_spec`` /
    ``problem_statement`` rather than ``spec`` / ``description``; fall back
    across all of them so a proposal is never silently dropped for an empty spec
    — the cause of the "claimed 0 proposals" handoff failure. The structured
    fields are rendered as readable sections (this text is what the coding agent
    executes and what the PR body shows), never a raw JSON dump.
    """
    prose = ""
    for key in ("proposed_solution", "spec", "description", "problem_statement"):
        prose = _text(proposal.get(key))
        if prose:
            break
    sections: list[str] = []
    problem = _text(proposal.get("problem_statement"))
    if prose:
        if problem and problem != prose:
            sections.append(f"## Problem\n{problem}")
            sections.append(f"## What to build\n{prose}")
        else:
            sections.append(prose)

    notes = _impl_notes(proposal.get("implementation_spec"))
    if notes:
        sections.append(f"## Implementation notes\n{notes}")
    criteria = _acceptance_criteria(proposal.get("success_criteria"))
    if criteria:
        sections.append(
            "## Acceptance criteria\n" + "\n".join(f"- {c}" for c in criteria)
        )
    return "\n\n".join(sections)


async def enqueue_proposals(
    pool: asyncpg.Pool, run_id: str, project_id: str, proposals: list[dict[str, Any]]
) -> int:
    """Insert approved proposals, idempotent on the project-scoped proposal id."""
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
            status = await conn.execute(
                """
                INSERT INTO feature_proposals
                    (project_id, proposal_id, run_id, status, title, spec, priority)
                VALUES ($1, $2, $3, 'approved', $4, $5, $6)
                ON CONFLICT (project_id, proposal_id) DO NOTHING
                """,
                project_id,
                proposal_id,
                run_id,
                title,
                spec,
                str(proposal.get("priority") or ""),
            )
            # "INSERT 0 0" means the row already existed (conflict skipped) —
            # counting it would make the return value lie on retries.
            if status.endswith(" 1"):
                inserted += 1
    return inserted


async def claim_proposals(
    pool: asyncpg.Pool,
    project_id: str,
    run_id: str,
    limit: int = 5,
    proposal_id: str | None = None,
) -> list[dict[str, Any]]:
    """Claim up to ``limit`` approved proposals, marking them ``implementing``.

    ``FOR UPDATE SKIP LOCKED`` makes concurrent claims disjoint, so the drain is
    safe to run from both the approval kick and a scheduled sweep. ``run_id``
    records the exact implementing run so expiry recovery can reopen only work
    abandoned by that run. When
    ``proposal_id`` is given, claim only that proposal (one PR per approved
    proposal); the ``status='approved'`` guard still applies, so an
    proposal already claimed by this same run is returned again so crash
    recovery can reconstruct its approval gate. Another run's claim remains
    excluded.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            if proposal_id is not None:
                rows = await conn.fetch(
                    """
                    SELECT proposal_id, title, spec, priority
                    FROM feature_proposals
                    WHERE project_id = $1
                      AND proposal_id = $2
                      AND (
                          status = 'approved'
                          OR (status = 'implementing' AND claim_run_id = $3)
                      )
                    FOR UPDATE SKIP LOCKED
                    """,
                    project_id,
                    proposal_id,
                    run_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT proposal_id, title, spec, priority
                    FROM feature_proposals
                    WHERE project_id = $1
                      AND (
                          status = 'approved'
                          OR (status = 'implementing' AND claim_run_id = $3)
                      )
                    ORDER BY created_at
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                    """,
                    project_id,
                    limit,
                    run_id,
                )
            claimed = [dict(r) for r in rows]
            if claimed:
                await conn.execute(
                    """
                    UPDATE feature_proposals
                    SET status = 'implementing', claim_run_id = $3,
                        error = NULL, updated_at = now()
                    WHERE project_id = $1
                      AND proposal_id = ANY($2::text[])
                    """,
                    project_id,
                    [c["proposal_id"] for c in claimed],
                    run_id,
                )
    return claimed


async def list_recent_proposals(
    pool: asyncpg.Pool, project_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent proposals (any status) for grounding the next proposal run.

    The feature-proposal prompt shows these as "already proposed or in flight"
    so the LLM stops re-proposing the same three themes every run — insights
    barely change between runs, so without this list every run rediscovers the
    same features and each becomes a duplicate PR.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT proposal_id, title, status
            FROM feature_proposals
            WHERE project_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            project_id,
            limit,
        )
    return [dict(r) for r in rows]


async def get_proposal(
    pool: asyncpg.Pool, project_id: str, proposal_id: str
) -> dict[str, Any] | None:
    """Fetch one project's durable proposal row (title/spec/status).

    The durable ``feature_proposals`` queue is the source of truth (D2), so the
    approval handler can open a PR for a gated changeset even when the persisted
    gate item predates the self-describing title/spec fields.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT proposal_id, project_id, title, spec, status FROM feature_proposals "
            "WHERE project_id = $1 AND proposal_id = $2",
            project_id,
            proposal_id,
        )
    return dict(row) if row is not None else None


async def mark_implemented(
    pool: asyncpg.Pool,
    project_id: str,
    proposal_id: str,
    changeset_id: str,
    run_id: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE feature_proposals
            SET status = 'implemented', changeset_id = $3,
                claim_run_id = NULL, updated_at = now()
            WHERE project_id = $1
              AND proposal_id = $2
              AND status = 'implementing'
              AND claim_run_id = $4
            """,
            project_id,
            proposal_id,
            changeset_id,
            run_id,
        )


async def mark_failed(
    pool: asyncpg.Pool,
    project_id: str,
    proposal_id: str,
    error: str,
    run_id: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE feature_proposals
            SET status = 'failed', error = $3,
                claim_run_id = NULL, updated_at = now()
            WHERE project_id = $1
              AND proposal_id = $2
              AND status = 'implementing'
              AND claim_run_id = $4
            """,
            project_id,
            proposal_id,
            error,
            run_id,
        )
