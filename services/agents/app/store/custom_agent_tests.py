"""Replica-safe admission and audit records for custom-agent dry-runs.

Dry-runs do not create normal ``agent_runs`` or result rows, but they do spend
real query and LLM capacity. Admission is serialized per project in PostgreSQL
so multiple Agents replicas cannot bypass the concurrency or rate boundary.
"""

from __future__ import annotations

import os
from typing import Literal

import asyncpg


CUSTOM_AGENT_TEST_RATE_LIMIT = 5
CUSTOM_AGENT_TEST_RATE_WINDOW_SECONDS = 60 * 60
# One tool loop may make 16 tool-enabled completions plus one forced final
# completion. Each completion can traverse all four provider rungs, each with
# the configured request timeout. Add an hour for bounded query calls,
# scheduling, and audit writes so a legal live run cannot be reaped early.
_MAX_LLM_COMPLETIONS = 16 + 1
_MAX_PROVIDER_ATTEMPTS = 4
_TOOL_AND_SCHEDULING_BUFFER_SECONDS = 60 * 60
CUSTOM_AGENT_TEST_LEASE_SECONDS = int(
    float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120"))
    * _MAX_LLM_COMPLETIONS
    * _MAX_PROVIDER_ATTEMPTS
    + _TOOL_AND_SCHEDULING_BUFFER_SECONDS
)


class CustomAgentTestBusyError(Exception):
    """Another dry-run currently owns this project's single-flight lease."""


class CustomAgentTestRateLimitError(Exception):
    """The project exhausted its bounded dry-run allowance."""


async def begin_custom_agent_test_run(
    pool: asyncpg.Pool,
    *,
    test_run_id: str,
    project_id: str,
    agent_slug: str,
    model_tier: str,
    time_range_days: int,
    max_tool_steps: int,
    allowed_tool_count: int,
    configured_preset_count: int,
) -> None:
    """Atomically claim one project dry-run and record its cost envelope.

    The transaction-scoped advisory lock is shared by every replica. The
    partial unique index on running rows is an independent database backstop.
    Every attempt admitted in the rolling window counts, including failures.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                project_id,
            )
            await conn.execute(
                """
                UPDATE custom_agent_test_runs
                SET status = 'failed',
                    error = 'dry-run lease expired before completion',
                    finished_at = now()
                WHERE project_id = $1
                  AND status = 'running'
                  AND lease_expires_at <= now()
                """,
                project_id,
            )
            running = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM custom_agent_test_runs
                    WHERE project_id = $1 AND status = 'running'
                )
                """,
                project_id,
            )
            if running:
                raise CustomAgentTestBusyError(
                    "A custom-agent test is already running for this project."
                )

            recent_count = await conn.fetchval(
                """
                SELECT count(*)
                FROM custom_agent_test_runs
                WHERE project_id = $1
                  AND started_at >= now() - make_interval(secs => $2)
                """,
                project_id,
                CUSTOM_AGENT_TEST_RATE_WINDOW_SECONDS,
            )
            if int(recent_count or 0) >= CUSTOM_AGENT_TEST_RATE_LIMIT:
                raise CustomAgentTestRateLimitError(
                    f"At most {CUSTOM_AGENT_TEST_RATE_LIMIT} custom-agent tests "
                    "may start per project per hour."
                )

            try:
                await conn.execute(
                    """
                    INSERT INTO custom_agent_test_runs (
                        test_run_id, project_id, agent_slug, model_tier,
                        time_range_days, max_tool_steps, allowed_tool_count,
                        configured_preset_count, lease_expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8,
                        now() + make_interval(secs => $9)
                    )
                    """,
                    test_run_id,
                    project_id,
                    agent_slug,
                    model_tier,
                    time_range_days,
                    max_tool_steps,
                    allowed_tool_count,
                    configured_preset_count,
                    CUSTOM_AGENT_TEST_LEASE_SECONDS,
                )
            except asyncpg.UniqueViolationError as exc:
                # The partial unique index is the final defense if admission
                # code with a different lock discipline reaches this database.
                raise CustomAgentTestBusyError(
                    "A custom-agent test is already running for this project."
                ) from exc


async def finish_custom_agent_test_run(
    pool: asyncpg.Pool,
    test_run_id: str,
    *,
    status: Literal["succeeded", "failed"],
    preset_tool_calls: int,
    agentic_tool_calls: int,
    llm_calls: int,
    llm_latency_ms: int | None,
    total_latency_ms: int,
    error: str | None = None,
) -> None:
    """Terminalize the durable dry-run audit row with actual work performed."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE custom_agent_test_runs
            SET status = $2,
                preset_tool_calls = $3,
                agentic_tool_calls = $4,
                llm_calls = $5,
                llm_latency_ms = $6,
                total_latency_ms = $7,
                error = $8,
                finished_at = now()
            WHERE test_run_id = $1 AND status = 'running'
            """,
            test_run_id,
            status,
            preset_tool_calls,
            agentic_tool_calls,
            llm_calls,
            llm_latency_ms,
            total_latency_ms,
            error[:4_000] if error is not None else None,
        )
