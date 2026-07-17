"""Workflow telemetry and fail-closed audit primitives.

Ordinary supervisor/tool observations are best-effort diagnostics. Human
decisions and mutation intents are authoritative only when inserted by the
approval outbox transaction, which must commit before external work is leased.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


def _row_to_audit_entry(row) -> dict:
    config = row["config"]
    safety = row["safety_result"]
    if isinstance(config, str):
        config = json.loads(config)
    if isinstance(safety, str):
        safety = json.loads(safety)
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "action_type": row["action_type"],
        "config": config,
        "safety_result": safety,
        "approval_status": row["approval_status"],
        "created_at": row["created_at"].isoformat(),
    }


class AuditLogger:
    """Write non-authoritative observations or explicitly required audit rows.

    Every significant action taken by the agent system is recorded with:
    - run_id: Which agent run triggered the action.
    - action_type: What kind of action (e.g., "create_experiment", "rollback").
    - config: The full configuration or parameters of the action.
    - safety_result: The safety validation outcome, if applicable.
    - approval_status: Whether human approval was granted/denied/pending.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def log(
        self,
        run_id: str,
        action_type: str,
        config: dict[str, Any] | None = None,
        safety_result: dict[str, Any] | None = None,
        approval_status: str | None = None,
    ) -> int:
        """Write best-effort, non-authoritative telemetry.

        This method is only for observations whose loss cannot change whether
        a mutation is allowed. Authoritative mutation paths must use
        :meth:`log_required` or insert their audit intent in the same database
        transaction as their command/outbox row.

        Args:
            run_id: The agent run ID.
            action_type: Type of action being logged.
            config: Action configuration/parameters.
            safety_result: Result from SafetyValidator, if applicable.
            approval_status: "approved", "rejected", "pending", or None.

        Returns:
            The auto-generated audit log entry ID.
        """
        try:
            entry_id = await self.log_required(
                run_id,
                action_type,
                config,
                safety_result,
                approval_status,
            )
            logger.debug(
                "Audit log entry %d: run=%s action=%s",
                entry_id, run_id, action_type,
            )
            return entry_id
        except Exception as exc:
            # Audit logging should never break the main flow
            logger.error("Failed to write audit log: %s", exc)
            return -1

    async def log_required(
        self,
        run_id: str,
        action_type: str,
        config: dict[str, Any] | None = None,
        safety_result: dict[str, Any] | None = None,
        approval_status: str | None = None,
        *,
        idempotency_key: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> int:
        """Persist an authoritative audit row or raise without authorizing work."""
        config_json = json.dumps(config or {}, default=str)
        safety_json = json.dumps(safety_result or {}, default=str)
        async with self._pool.acquire() as conn:
            entry_id = await conn.fetchval(
                """
                INSERT INTO agent_audit_log (
                    run_id, action_type, config, safety_result, approval_status,
                    idempotency_key, correlation_id
                )
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7)
                RETURNING id
                """,
                run_id,
                action_type,
                config_json,
                safety_json,
                approval_status,
                idempotency_key,
                correlation_id,
            )
        logger.debug(
            "Required audit log entry %d: run=%s action=%s",
            entry_id,
            run_id,
            action_type,
        )
        return entry_id

    async def get_run_audit_trail(
        self,
        run_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Retrieve the complete audit trail for a run.

        Args:
            run_id: The agent run ID to query.
            limit: Maximum entries to return.

        Returns:
            List of audit log entries, newest first.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, run_id, action_type, config, safety_result,
                       approval_status, created_at
                FROM agent_audit_log
                WHERE run_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                run_id,
                limit,
            )

        return [_row_to_audit_entry(row) for row in rows]

    async def get_recent_actions(
        self,
        project_id: str | None = None,
        action_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Query recent audit log entries with optional filters.

        Args:
            project_id: Filter by project (via join with agent_runs).
            action_type: Filter by action type.
            limit: Maximum entries to return.

        Returns:
            List of audit log entries, newest first.
        """
        conditions = ["1=1"]
        params: list[Any] = []

        if action_type:
            params.append(action_type)
            conditions.append(f"al.action_type = ${len(params)}")

        if project_id is not None:
            params.append(project_id)
            conditions.append(f"ar.project_id = ${len(params)}")

        params.append(limit)
        limit_param = f"${len(params)}"

        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT al.id, al.run_id, al.action_type, al.config,
                   al.safety_result, al.approval_status, al.created_at
            FROM agent_audit_log al
            LEFT JOIN agent_runs ar ON al.run_id = ar.run_id
            WHERE {where_clause}
            ORDER BY al.created_at DESC
            LIMIT {limit_param}
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [_row_to_audit_entry(row) for row in rows]
