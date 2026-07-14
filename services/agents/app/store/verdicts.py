"""Persistence for experiment verdicts — the evaluation agent's durable output.

A verdict row is the loop's memory of *what happened* to an experiment:
ship / rollback / iterate / extend / immature, with the reasoning and the
result numbers that justified it. ``ship`` verdicts additionally act as the
work queue for the reshaped feature_proposal agent (phase 4): each unconsumed
ship verdict becomes one durable-feature proposal, then is marked consumed so
reruns never propose the same win twice.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

VALID_VERDICTS = ("ship", "rollback", "iterate", "extend", "immature")


async def record_verdict(
    pool: asyncpg.Pool,
    project_id: str,
    run_id: str,
    experiment_id: str,
    verdict: str,
    reasoning: str = "",
    results: dict[str, Any] | None = None,
    durable_feature: str = "",
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO experiment_verdicts
                (project_id, experiment_id, run_id, verdict, reasoning, results, durable_feature)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            project_id,
            experiment_id,
            run_id,
            verdict,
            reasoning,
            json.dumps(results or {}, default=str),
            durable_feature,
        )


async def list_verdicts(
    pool: asyncpg.Pool, project_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    """The project's verdict history, newest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, experiment_id, verdict, reasoning, results, durable_feature,
                   consumed, created_at
            FROM experiment_verdicts
            WHERE project_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            project_id,
            limit,
        )
    return [_row(r) for r in rows]


async def list_unconsumed_ship_verdicts(
    pool: asyncpg.Pool, project_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Ship verdicts not yet turned into a durable feature proposal (oldest first)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, experiment_id, verdict, reasoning, results, durable_feature,
                   consumed, created_at
            FROM experiment_verdicts
            WHERE project_id = $1 AND verdict = 'ship' AND consumed = FALSE
            ORDER BY created_at ASC
            LIMIT $2
            """,
            project_id,
            limit,
        )
    return [_row(r) for r in rows]


async def mark_verdicts_consumed(pool: asyncpg.Pool, verdict_ids: list[int]) -> None:
    if not verdict_ids:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE experiment_verdicts SET consumed = TRUE WHERE id = ANY($1::bigint[])",
            verdict_ids,
        )


def _row(record: Any) -> dict[str, Any]:
    row = dict(record)
    results = row.get("results")
    if isinstance(results, str):
        try:
            row["results"] = json.loads(results)
        except (json.JSONDecodeError, ValueError):
            row["results"] = {}
    return row
