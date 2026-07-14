"""Fail-fast validation for the Config service's migrated schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MIGRATION_VERSION = 6
MIGRATION_NAME = "006_config.sql"
REQUIRED_COLUMNS = frozenset(
    {
        ("flags", "key"),
        ("flags", "project_id"),
        ("flags", "state"),
        ("flags", "enabled"),
        ("flags", "default_variant"),
        ("flags", "variants"),
        ("flags", "rules"),
        ("flags", "fallthrough"),
        ("flags", "evaluation_mode"),
        ("flags", "guardrails"),
        ("flags", "version"),
        ("flag_audit_log", "project_id"),
        ("flag_audit_log", "flag_key"),
        ("flag_audit_log", "evidence"),
        ("experiments", "key"),
        ("experiments", "project_id"),
        ("experiments", "status"),
        ("experiments", "flag_key"),
        ("experiments", "default_variant"),
        ("experiments", "variants_json"),
        ("experiments", "targeting_rules_json"),
        ("experiments", "primary_metric_json"),
    }
)


async def assert_schema_ready(conn: Any) -> None:
    """Reject startup unless the checksummed Config migration is present."""
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
            f"Config PostgreSQL schema is incomplete ({formatted}); "
            "restore the database or apply migrations before startup"
        )
