"""Fail-fast validation for the Agents service's migrated schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.memory.embeddings import EMBEDDING_DIMENSIONS


MIGRATION_VERSION = 20
MIGRATION_NAME = "020_agents_governance.sql"
REQUIRED_COLUMNS = frozenset(
    {
        ("admin_projects", "created_by"),
        ("agent_memory", "project_id"),
        ("agent_memory", "embedding"),
        ("agent_runs", "run_id"),
        ("agent_runs", "project_id"),
        ("agent_runs", "status"),
        ("agent_runs", "phase"),
        ("agent_runs", "lease_owner_id"),
        ("agent_runs", "lease_expires_at"),
        ("agent_audit_log", "run_id"),
        ("agent_audit_log", "schema_version"),
        ("agent_audit_log", "occurred_at"),
        ("agent_run_results", "run_id"),
        ("feature_proposals", "proposal_id"),
        ("feature_proposals", "project_id"),
        ("feature_proposals", "status"),
        ("feature_proposals", "claim_run_id"),
        ("designed_experiments", "experiment_id"),
        ("designed_experiments", "changeset_id"),
        ("experiment_verdicts", "experiment_id"),
        ("custom_agents", "agent_id"),
        ("custom_agents", "max_tool_steps"),
        ("custom_agent_test_runs", "test_run_id"),
        ("custom_agent_test_runs", "project_id"),
        ("custom_agent_test_runs", "status"),
        ("custom_agent_test_runs", "llm_calls"),
        ("custom_agent_test_runs", "lease_expires_at"),
        ("llm_calls", "project_id"),
        ("llm_calls", "run_id"),
    }
)


async def assert_schema_ready(conn: Any) -> None:
    """Reject startup when migrations or the configured vector shape drift."""
    ledger_exists = await conn.fetchval(
        "SELECT to_regclass('public.apdl_schema_migrations') IS NOT NULL"
    )
    if not ledger_exists:
        raise RuntimeError(
            "PostgreSQL migration ledger is missing; run `make migrate-postgres`"
        )

    applied_name = await conn.fetchval(
        "SELECT name FROM apdl_schema_migrations WHERE version = $1",
        MIGRATION_VERSION,
    )
    if applied_name != MIGRATION_NAME:
        raise RuntimeError(
            f"Required PostgreSQL migration {MIGRATION_NAME} is not applied; "
            "run `make migrate-postgres`"
        )

    tables = sorted({table for table, _ in REQUIRED_COLUMNS})
    rows: list[Mapping[str, str]] = await conn.fetch(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY($1::text[])
        """,
        tables,
    )
    available = {(row["table_name"], row["column_name"]) for row in rows}
    missing = sorted(REQUIRED_COLUMNS - available)
    if missing:
        formatted = ", ".join(f"{table}.{column}" for table, column in missing)
        raise RuntimeError(
            f"Agents PostgreSQL schema is incomplete ({formatted}); "
            "restore the database or apply migrations before startup"
        )

    current_dimension = await conn.fetchval(
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = 'public.agent_memory'::regclass
          AND attname = 'embedding' AND NOT attisdropped
        """
    )
    if current_dimension != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            "agent_memory.embedding is "
            f"vector({current_dimension}), but the configured model requires "
            f"vector({EMBEDDING_DIMENSIONS}); add and apply an explicit migration"
        )
