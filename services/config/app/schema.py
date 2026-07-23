"""Fail-fast validation for the Config service's migrated schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MIGRATION_VERSION = 42
MIGRATION_NAME = "042_variant_weight_contract.sql"
REQUIRED_CONSTRAINTS = frozenset(
    {
        ("flags", "flags_variants_canonical_check"),
        ("experiments", "experiments_variants_canonical_check"),
    }
)
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
        ("flags", "auto_disable"),
        ("flags", "guardrails"),
        ("flags", "version"),
        ("flag_audit_log", "project_id"),
        ("flag_audit_log", "flag_key"),
        ("flag_audit_log", "evidence"),
        ("flag_audit_log", "origin"),
        ("experiments", "key"),
        ("experiments", "project_id"),
        ("experiments", "status"),
        ("experiments", "flag_key"),
        ("experiments", "default_variant"),
        ("experiments", "variants_json"),
        ("experiments", "targeting_rules_json"),
        ("experiments", "primary_metric_json"),
        ("experiments", "statistical_plan"),
        ("experiments", "minimum_exposure_config_version"),
        ("experiments", "creation_idempotency_key"),
        ("experiments", "creation_idempotency_request_sha256"),
        ("experiments", "start_date"),
        ("experiments", "end_date"),
        ("experiments", "version"),
        ("experiments", "archived_at"),
        ("experiments", "archived_by"),
        ("experiment_audit_log", "id"),
        ("experiment_audit_log", "project_id"),
        ("experiment_audit_log", "experiment_key"),
        ("experiment_audit_log", "action"),
        ("experiment_audit_log", "actor"),
        ("experiment_audit_log", "previous_version"),
        ("experiment_audit_log", "new_version"),
        ("experiment_audit_log", "before"),
        ("experiment_audit_log", "after"),
        ("experiment_audit_log", "created_at"),
        ("config_outbox", "id"),
        ("config_outbox", "project_id"),
        ("config_outbox", "kind"),
        ("config_outbox", "dedup_key"),
        ("config_outbox", "payload"),
        ("config_outbox", "attempts"),
        ("config_outbox", "available_at"),
        ("config_outbox", "claimed_at"),
        ("config_outbox", "processed_at"),
        ("config_outbox", "last_error"),
        ("config_outbox", "created_at"),
        ("config_outbox", "quarantined_at"),
        ("config_outbox", "failure_class"),
        ("config_outbox", "failure_code"),
        ("config_exposure_receipts", "project_id"),
        ("config_exposure_receipts", "message_id"),
        ("config_exposure_receipts", "canonical_payload"),
        ("config_exposure_receipts", "first_seen_at"),
        ("config_exposure_receipts", "last_seen_at"),
        ("config_project_versions", "project_id"),
        ("config_project_versions", "project_version"),
        ("config_project_versions", "updated_at"),
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

    constraint_rows: list[Mapping[str, Any]] = await conn.fetch(
        """
        SELECT
            table_record.relname AS table_name,
            constraint_record.conname AS constraint_name,
            constraint_record.convalidated AS constraint_validated
        FROM pg_catalog.pg_constraint AS constraint_record
        JOIN pg_catalog.pg_class AS table_record
          ON table_record.oid = constraint_record.conrelid
        JOIN pg_catalog.pg_namespace AS table_namespace
          ON table_namespace.oid = table_record.relnamespace
        WHERE table_namespace.nspname = 'public'
          AND table_record.relname = ANY($1::text[])
          AND constraint_record.conname = ANY($2::text[])
        """,
        sorted({table for table, _ in REQUIRED_CONSTRAINTS}),
        sorted({constraint for _, constraint in REQUIRED_CONSTRAINTS}),
    )
    available_constraints = {
        (row["table_name"], row["constraint_name"])
        for row in constraint_rows
        if row["constraint_validated"] is True
    }
    missing_constraints = sorted(
        REQUIRED_CONSTRAINTS - available_constraints
    )
    if missing_constraints:
        formatted = ", ".join(
            f"{table}.{constraint}"
            for table, constraint in missing_constraints
        )
        raise RuntimeError(
            f"Config PostgreSQL variant constraints are incomplete "
            f"({formatted}); restore the database or apply migrations "
            "before startup"
        )
