"""Live PostgreSQL race checks for the per-project Agents execution lane.

Set ``APDL_AGENTS_LANE_TEST_POSTGRES_URL`` to a disposable, fully migrated
database owned by a superuser.  The normal unit suite does not require a live
database; release verification runs this test explicitly against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app.store.approval_effects import (
    RetryableApprovalEffectError,
    _claim_effect,
    _complete_effect,
    _fail_effect,
    _finalize_command_if_terminal,
)
from app.store.run_leases import cancel_run


POSTGRES_URL = os.getenv("APDL_AGENTS_LANE_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="APDL_AGENTS_LANE_TEST_POSTGRES_URL is not configured",
)


async def _insert_run(
    conn: asyncpg.Connection,
    *,
    run_id: str,
    project_id: str,
    status: str,
    phase: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO agent_runs (
            run_id, project_id, trigger_type, autonomy_level, status, phase, config
        )
        VALUES ($1, $2, 'manual', 2, $3, $4, $5::jsonb)
        """,
        run_id,
        project_id,
        status,
        phase,
        '{"analysis_types":["behavior_analysis"],"time_range_days":7}',
    )


async def _competing_fresh_insert(
    conn: asyncpg.Connection,
    *,
    run_id: str,
    project_id: str,
) -> None:
    async with conn.transaction():
        # The execution-authority trigger is orthogonal to this isolated lane
        # race.  Constraints, generated columns, and unique indexes stay active.
        await conn.execute("SET LOCAL session_replication_role = replica")
        await _insert_run(
            conn,
            run_id=run_id,
            project_id=project_id,
            status="started",
            phase="initializing",
        )


async def _wait_for_blocked_query(
    conn: asyncpg.Connection,
    *,
    application_name: str,
    query_fragment: str,
) -> None:
    for _ in range(100):
        blocked = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_stat_activity
                WHERE application_name = $1
                  AND wait_event_type = 'Lock'
                  AND position($2 IN query) > 0
            )
            """,
            application_name,
            query_fragment,
        )
        if blocked:
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"query did not block on a lock: {query_fragment}")


@pytest.mark.asyncio
async def test_fresh_run_cannot_cross_waiting_or_approval_resume_boundaries() -> None:
    assert POSTGRES_URL is not None
    waiting = await asyncpg.connect(POSTGRES_URL)
    competing = await asyncpg.connect(POSTGRES_URL)
    observer = await asyncpg.connect(POSTGRES_URL)
    suffix = uuid.uuid4().hex
    project_id = f"lane{suffix[:24]}"
    waiting_run_id = f"waiting-{suffix}"
    first_fresh_run_id = f"fresh-a-{suffix}"
    approval_fresh_run_id = f"fresh-b-{suffix}"
    resume_fresh_run_id = f"fresh-c-{suffix}"
    replacement_run_id = f"replacement-{suffix}"

    try:
        applied_name = await observer.fetchval(
            "SELECT name FROM apdl_schema_migrations WHERE version = 34"
        )
        assert applied_name == "034_agent_project_execution_lane.sql"

        waiting_tx = waiting.transaction()
        await waiting_tx.start()
        await waiting.execute("SET LOCAL session_replication_role = replica")
        await _insert_run(
            waiting,
            run_id=waiting_run_id,
            project_id=project_id,
            status="waiting_approval",
            phase="experiment_design_approval",
        )

        fresh_vs_waiting = asyncio.create_task(
            _competing_fresh_insert(
                competing,
                run_id=first_fresh_run_id,
                project_id=project_id,
            )
        )
        await asyncio.sleep(0.1)
        assert not fresh_vs_waiting.done()
        await waiting_tx.commit()
        with pytest.raises(
            asyncpg.UniqueViolationError,
            match="agent_runs_one_execution_lane_per_project_idx",
        ):
            await asyncio.wait_for(fresh_vs_waiting, timeout=5)

        async with waiting.transaction():
            await waiting.execute("SET LOCAL session_replication_role = replica")
            await waiting.execute(
                """
                UPDATE agent_runs
                SET status = 'approval_queued', updated_at = now()
                WHERE run_id = $1
                """,
                waiting_run_id,
            )

        with pytest.raises(
            asyncpg.UniqueViolationError,
            match="agent_runs_one_execution_lane_per_project_idx",
        ):
            await _competing_fresh_insert(
                competing,
                run_id=approval_fresh_run_id,
                project_id=project_id,
            )

        resume_tx = waiting.transaction()
        await resume_tx.start()
        await waiting.execute("SET LOCAL session_replication_role = replica")
        await waiting.execute(
            """
            UPDATE agent_runs
            SET status = 'approved', phase = 'resuming', updated_at = now()
            WHERE run_id = $1
            """,
            waiting_run_id,
        )

        fresh_vs_resume = asyncio.create_task(
            _competing_fresh_insert(
                competing,
                run_id=resume_fresh_run_id,
                project_id=project_id,
            )
        )
        await asyncio.sleep(0.1)
        assert not fresh_vs_resume.done()
        await resume_tx.commit()
        with pytest.raises(
            asyncpg.UniqueViolationError,
            match="agent_runs_one_execution_lane_per_project_idx",
        ):
            await asyncio.wait_for(fresh_vs_resume, timeout=5)

        assert (
            await observer.fetchval(
                "SELECT execution_lane_project_id FROM agent_runs WHERE run_id = $1",
                waiting_run_id,
            )
            == project_id
        )

        async with waiting.transaction():
            await waiting.execute("SET LOCAL session_replication_role = replica")
            await waiting.execute(
                """
                UPDATE agent_runs
                SET status = 'completed', phase = 'done', updated_at = now()
                WHERE run_id = $1
                """,
                waiting_run_id,
            )

        await _competing_fresh_insert(
            competing,
            run_id=replacement_run_id,
            project_id=project_id,
        )
        lanes: list[dict[str, Any]] = [
            dict(row)
            for row in await observer.fetch(
                """
                SELECT run_id, execution_lane_project_id
                FROM agent_runs
                WHERE project_id = $1
                ORDER BY run_id
                """,
                project_id,
            )
        ]
        assert lanes == [
            {
                "run_id": replacement_run_id,
                "execution_lane_project_id": project_id,
            },
            {"run_id": waiting_run_id, "execution_lane_project_id": None},
        ]
    finally:
        for task_name in ("fresh_vs_waiting", "fresh_vs_resume"):
            task = locals().get(task_name)
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await waiting.close()
        await competing.close()
        async with observer.transaction():
            await observer.execute("SET LOCAL session_replication_role = replica")
            await observer.execute(
                "DELETE FROM agent_runs WHERE project_id = $1",
                project_id,
            )
        await observer.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_claim_and_retains_lane_until_effect_settles() -> None:
    assert POSTGRES_URL is not None
    application_name = f"ra11-race-{uuid.uuid4().hex}"
    pool = await asyncpg.create_pool(
        POSTGRES_URL,
        min_size=2,
        max_size=4,
        server_settings={"application_name": application_name},
    )
    blocker = await asyncpg.connect(POSTGRES_URL)
    observer = await asyncpg.connect(POSTGRES_URL)
    suffix = uuid.uuid4().hex
    project_id = f"cancel{suffix[:22]}"
    run_id = f"cancel-run-{suffix}"
    replacement_run_id = f"cancel-replacement-{suffix}"
    command_id = uuid.uuid4()
    effect_id = uuid.uuid4()
    effect_key = f"{command_id}:{effect_id}"
    blocker_tx: asyncpg.Transaction | None = None
    claim_task: asyncio.Task[Any] | None = None
    cancel_task: asyncio.Task[Any] | None = None

    try:
        async with observer.transaction():
            await observer.execute("SET LOCAL session_replication_role = replica")
            await _insert_run(
                observer,
                run_id=run_id,
                project_id=project_id,
                status="approval_queued",
                phase="code_implementation_approval",
            )
            await observer.execute(
                """
                INSERT INTO agent_approval_commands (
                    command_id, run_id, project_id, actor_credential_id,
                    request_sha256, gate_id, gate_agent, status, resume_status,
                    approved_count, rejected_count
                )
                VALUES (
                    $1, $2, $3, 'test-agents', $4, $5,
                    'code_implementation', 'queued', 'approved', 1, 0
                )
                """,
                command_id,
                run_id,
                project_id,
                "a" * 64,
                f"{run_id}:code_implementation",
            )
            await observer.execute(
                """
                INSERT INTO agent_approval_effects (
                    effect_id, command_id, run_id, project_id, item_id,
                    effect_type, effect_order, payload, idempotency_key
                )
                VALUES (
                    $1, $2, $3, $4, 'proposal-1',
                    'record_proposal_rejection', 0, '{}'::jsonb, $5
                )
                """,
                effect_id,
                command_id,
                run_id,
                project_id,
                effect_key,
            )

        blocker_tx = blocker.transaction()
        await blocker_tx.start()
        await blocker.fetchval(
            """
            SELECT effect_id
            FROM agent_approval_effects
            WHERE effect_id = $1
            FOR UPDATE
            """,
            effect_id,
        )

        claim_task = asyncio.create_task(
            _claim_effect(pool, "race-worker", lease_seconds=60)
        )
        await _wait_for_blocked_query(
            observer,
            application_name=application_name,
            query_fragment="UPDATE agent_approval_effects AS effect",
        )

        cancel_task = asyncio.create_task(
            cancel_run(
                pool,
                run_id=run_id,
                project_id=project_id,
                actor_credential_id="test-agents",
            )
        )
        await _wait_for_blocked_query(
            observer,
            application_name=application_name,
            query_fragment="SELECT status, phase, execution_lane_project_id",
        )
        assert not claim_task.done()
        assert not cancel_task.done()

        await blocker_tx.commit()
        blocker_tx = None
        claimed = await asyncio.wait_for(claim_task, timeout=5)
        assert claimed is not None
        cancelled = await asyncio.wait_for(cancel_task, timeout=5)
        assert cancelled.status == "cancelling"

        run = await observer.fetchrow(
            """
            SELECT status, phase, execution_lane_project_id
            FROM agent_runs
            WHERE run_id = $1
            """,
            run_id,
        )
        assert dict(run) == {
            "status": "cancelling",
            "phase": "cancellation_draining",
            "execution_lane_project_id": project_id,
        }

        with pytest.raises(
            asyncpg.CheckViolationError,
            match="cannot release.*execution lane",
        ):
            await observer.execute(
                "UPDATE agent_runs SET status = 'cancelled' WHERE run_id = $1",
                run_id,
            )

        with pytest.raises(
            asyncpg.UniqueViolationError,
            match="agent_runs_one_execution_lane_per_project_idx",
        ):
            await _competing_fresh_insert(
                observer,
                run_id=replacement_run_id,
                project_id=project_id,
            )

        await _fail_effect(
            pool,
            claimed,
            RetryableApprovalEffectError(
                "downstream outcome requires reconciliation",
                delay_seconds=0,
            ),
        )
        reconciling = await observer.fetchrow(
            """
            SELECT status, phase, execution_lane_project_id
            FROM agent_runs
            WHERE run_id = $1
            """,
            run_id,
        )
        assert dict(reconciling) == {
            "status": "cancelling",
            "phase": "cancellation_reconciliation",
            "execution_lane_project_id": project_id,
        }

        reclaimed = await _claim_effect(
            pool,
            "reconciliation-worker",
            lease_seconds=60,
        )
        assert reclaimed is not None
        assert reclaimed.effect_id == claimed.effect_id
        assert reclaimed.idempotency_key == claimed.idempotency_key
        assert reclaimed.attempt_count == 2

        await _complete_effect(
            pool,
            reclaimed,
            {"proposal_id": "proposal-1", "status": "failed"},
        )
        settled = await observer.fetchrow(
            """
            SELECT status, phase, execution_lane_project_id
            FROM agent_runs
            WHERE run_id = $1
            """,
            run_id,
        )
        assert dict(settled) == {
            "status": "cancelled",
            "phase": "cancelled",
            "execution_lane_project_id": None,
        }

        with pytest.raises(
            asyncpg.CheckViolationError,
            match="cannot become live without an active project execution lane",
        ):
            await observer.execute(
                """
                UPDATE agent_approval_effects
                SET status = 'retryable_failed'
                WHERE effect_id = $1
                """,
                effect_id,
            )

        await _competing_fresh_insert(
            observer,
            run_id=replacement_run_id,
            project_id=project_id,
        )
    finally:
        for task in (claim_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if blocker_tx is not None:
            await blocker_tx.rollback()
        await pool.close()
        await blocker.close()
        async with observer.transaction():
            await observer.execute("SET LOCAL session_replication_role = replica")
            await observer.execute(
                "DELETE FROM agent_audit_log WHERE run_id IN ($1, $2)",
                run_id,
                replacement_run_id,
            )
            await observer.execute(
                "DELETE FROM agent_runs WHERE project_id = $1",
                project_id,
            )
        await observer.close()


@pytest.mark.asyncio
async def test_terminal_sibling_does_not_abandon_retryable_effect() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=2)
    observer = await asyncpg.connect(POSTGRES_URL)
    suffix = uuid.uuid4().hex
    project_id = f"siblings{suffix[:20]}"
    run_id = f"sibling-run-{suffix}"
    replacement_run_id = f"sibling-replacement-{suffix}"
    command_id = uuid.uuid4()
    terminal_effect_id = uuid.uuid4()
    retryable_effect_id = uuid.uuid4()

    try:
        async with observer.transaction():
            await observer.execute("SET LOCAL session_replication_role = replica")
            await _insert_run(
                observer,
                run_id=run_id,
                project_id=project_id,
                status="approval_queued",
                phase="code_implementation_approval",
            )
            await observer.execute(
                """
                INSERT INTO agent_approval_commands (
                    command_id, run_id, project_id, actor_credential_id,
                    request_sha256, gate_id, gate_agent, status, resume_status,
                    approved_count, rejected_count
                )
                VALUES (
                    $1, $2, $3, 'test-agents', $4, $5,
                    'code_implementation', 'processing', 'approved', 2, 0
                )
                """,
                command_id,
                run_id,
                project_id,
                "b" * 64,
                f"{run_id}:code_implementation",
            )
            await observer.executemany(
                """
                INSERT INTO agent_approval_effects (
                    effect_id, command_id, run_id, project_id, item_id,
                    effect_type, effect_order, payload, status,
                    idempotency_key, attempt_count, last_error, completed_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, 'record_proposal_rejection',
                    $6, '{}'::jsonb, $7, $8, 1, $9,
                    CASE WHEN $7 = 'manual_intervention' THEN now() ELSE NULL END
                )
                """,
                [
                    (
                        terminal_effect_id,
                        command_id,
                        run_id,
                        project_id,
                        "proposal-terminal",
                        0,
                        "manual_intervention",
                        f"{command_id}:{terminal_effect_id}",
                        "PermanentApprovalEffectError: invalid persisted payload",
                    ),
                    (
                        retryable_effect_id,
                        command_id,
                        run_id,
                        project_id,
                        "proposal-retryable",
                        10,
                        "retryable_failed",
                        f"{command_id}:{retryable_effect_id}",
                        "RetryableApprovalEffectError: downstream unavailable",
                    ),
                ],
            )

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval(
                    """
                    SELECT run_id
                    FROM agent_runs
                    WHERE run_id = $1 AND project_id = $2
                    FOR UPDATE
                    """,
                    run_id,
                    project_id,
                )
                await _finalize_command_if_terminal(conn, str(command_id))

        retained = await observer.fetchrow(
            """
            SELECT run.status, run.execution_lane_project_id,
                   command.status AS command_status,
                   effect.status AS effect_status
            FROM agent_runs AS run
            JOIN agent_approval_commands AS command ON command.run_id = run.run_id
            JOIN agent_approval_effects AS effect
              ON effect.command_id = command.command_id
             AND effect.effect_id = $2
            WHERE run.run_id = $1
            """,
            run_id,
            retryable_effect_id,
        )
        assert dict(retained) == {
            "status": "approval_queued",
            "execution_lane_project_id": project_id,
            "command_status": "processing",
            "effect_status": "retryable_failed",
        }

        with pytest.raises(
            asyncpg.UniqueViolationError,
            match="agent_runs_one_execution_lane_per_project_idx",
        ):
            await _competing_fresh_insert(
                observer,
                run_id=replacement_run_id,
                project_id=project_id,
            )

        claimed = await _claim_effect(pool, "sibling-reconciliation", lease_seconds=60)
        assert claimed is not None
        assert claimed.effect_id == str(retryable_effect_id)
        await _complete_effect(
            pool,
            claimed,
            {"proposal_id": "proposal-retryable", "status": "failed"},
        )

        released = await observer.fetchrow(
            """
            SELECT run.status, run.execution_lane_project_id,
                   command.status AS command_status,
                   effect.status AS effect_status
            FROM agent_runs AS run
            JOIN agent_approval_commands AS command ON command.run_id = run.run_id
            JOIN agent_approval_effects AS effect
              ON effect.command_id = command.command_id
             AND effect.effect_id = $2
            WHERE run.run_id = $1
            """,
            run_id,
            retryable_effect_id,
        )
        assert dict(released) == {
            "status": "manual_intervention",
            "execution_lane_project_id": None,
            "command_status": "manual_intervention",
            "effect_status": "succeeded",
        }
        await _competing_fresh_insert(
            observer,
            run_id=replacement_run_id,
            project_id=project_id,
        )
    finally:
        await pool.close()
        async with observer.transaction():
            await observer.execute("SET LOCAL session_replication_role = replica")
            await observer.execute(
                "DELETE FROM agent_audit_log WHERE run_id IN ($1, $2)",
                run_id,
                replacement_run_id,
            )
            await observer.execute(
                "DELETE FROM agent_runs WHERE project_id = $1",
                project_id,
            )
        await observer.close()
