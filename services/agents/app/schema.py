"""Fail-fast validation for the Agents service's migrated schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.memory.embeddings import EMBEDDING_DIMENSIONS


MIGRATION_VERSION = 34
MIGRATION_NAME = "034_agent_project_execution_lane.sql"
REQUIRED_COLUMNS = frozenset(
    {
        ("admin_projects", "created_by"),
        ("admin_project_execution_authorizations", "project_id"),
        ("admin_project_execution_authorizations", "authorization_source"),
        ("admin_project_execution_authorizations", "actor"),
        ("admin_project_execution_authorizations", "reason"),
        ("admin_project_execution_authorizations", "authorized_at"),
        ("auth_credentials", "actor_user_id"),
        ("agent_memory", "project_id"),
        ("agent_memory", "embedding"),
        ("agent_runs", "run_id"),
        ("agent_runs", "project_id"),
        ("agent_runs", "status"),
        ("agent_runs", "phase"),
        ("agent_runs", "execution_lane_project_id"),
        ("agent_runs", "lease_owner_id"),
        ("agent_runs", "lease_expires_at"),
        ("agent_runs", "config"),
        ("agent_runs", "updated_at"),
        ("agent_audit_log", "run_id"),
        ("agent_audit_log", "schema_version"),
        ("agent_audit_log", "occurred_at"),
        ("agent_run_results", "run_id"),
        ("agent_run_results", "agent_name"),
        ("agent_run_results", "produces"),
        ("agent_run_results", "output"),
        ("agent_run_results", "metadata"),
        ("agent_approval_commands", "command_id"),
        ("agent_approval_commands", "run_id"),
        ("agent_approval_commands", "project_id"),
        ("agent_approval_commands", "actor_credential_id"),
        ("agent_approval_commands", "actor_user_id"),
        ("agent_approval_commands", "request_sha256"),
        ("agent_approval_commands", "gate_id"),
        ("agent_approval_commands", "gate_agent"),
        ("agent_approval_commands", "status"),
        ("agent_approval_commands", "resume_status"),
        ("agent_approval_commands", "approved_count"),
        ("agent_approval_commands", "rejected_count"),
        ("agent_approval_commands", "comment"),
        ("agent_approval_commands", "last_error"),
        ("agent_approval_commands", "created_at"),
        ("agent_approval_commands", "updated_at"),
        ("agent_approval_commands", "completed_at"),
        ("agent_approval_decisions", "command_id"),
        ("agent_approval_decisions", "item_id"),
        ("agent_approval_decisions", "approved"),
        ("agent_approval_effects", "effect_id"),
        ("agent_approval_effects", "command_id"),
        ("agent_approval_effects", "run_id"),
        ("agent_approval_effects", "project_id"),
        ("agent_approval_effects", "item_id"),
        ("agent_approval_effects", "effect_type"),
        ("agent_approval_effects", "effect_order"),
        ("agent_approval_effects", "depends_on_effect_id"),
        ("agent_approval_effects", "payload"),
        ("agent_approval_effects", "status"),
        ("agent_approval_effects", "idempotency_key"),
        ("agent_approval_effects", "quota_action_type"),
        ("agent_approval_effects", "attempt_count"),
        ("agent_approval_effects", "max_attempts"),
        ("agent_approval_effects", "next_attempt_at"),
        ("agent_approval_effects", "lease_owner_id"),
        ("agent_approval_effects", "lease_expires_at"),
        ("agent_approval_effects", "result"),
        ("agent_approval_effects", "last_error"),
        ("agent_approval_effects", "created_at"),
        ("agent_approval_effects", "updated_at"),
        ("agent_approval_effects", "completed_at"),
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
        ("llm_calls", "call_id"),
        ("llm_calls", "execution_kind"),
        ("llm_calls", "execution_owner_id"),
        ("llm_calls", "purpose"),
        ("llm_calls", "data_classification"),
        ("llm_calls", "prompt_sha256"),
        ("llm_calls", "status"),
        ("llm_calls", "attempt_count"),
        ("llm_calls", "input_tokens"),
        ("llm_calls", "output_tokens"),
        ("llm_calls", "cost_usd_micros"),
        ("llm_calls", "error_classification"),
        ("llm_calls", "completed_at"),
        ("llm_project_policies", "project_id"),
        ("llm_project_policies", "required_data_residency"),
        ("llm_project_policies", "allow_cross_vendor_retry"),
        ("llm_project_policies", "project_daily_cost_limit_usd_micros"),
        ("llm_project_policies", "run_cost_limit_usd_micros"),
        ("llm_project_provider_policies", "project_id"),
        ("llm_project_provider_policies", "provider"),
        ("llm_project_provider_policies", "model"),
        ("llm_project_provider_policies", "endpoint_url"),
        ("llm_project_provider_policies", "data_residency"),
        ("llm_project_provider_policies", "allowed_data_classifications"),
        (
            "llm_project_provider_policies",
            "input_cost_per_million_tokens_usd_micros",
        ),
        (
            "llm_project_provider_policies",
            "output_cost_per_million_tokens_usd_micros",
        ),
        ("llm_project_provider_policies", "enabled"),
        ("llm_provider_attempts", "attempt_id"),
        ("llm_provider_attempts", "call_id"),
        ("llm_provider_attempts", "project_id"),
        ("llm_provider_attempts", "run_id"),
        ("llm_provider_attempts", "attempt_number"),
        ("llm_provider_attempts", "execution_owner_id"),
        ("llm_provider_attempts", "provider"),
        ("llm_provider_attempts", "model"),
        ("llm_provider_attempts", "endpoint_url"),
        ("llm_provider_attempts", "status"),
        ("llm_provider_attempts", "prompt_sha256"),
        ("llm_provider_attempts", "estimated_input_tokens"),
        ("llm_provider_attempts", "max_output_tokens"),
        ("llm_provider_attempts", "input_tokens"),
        ("llm_provider_attempts", "output_tokens"),
        ("llm_provider_attempts", "latency_ms"),
        ("llm_provider_attempts", "reserved_cost_usd_micros"),
        ("llm_provider_attempts", "charged_cost_usd_micros"),
        ("llm_provider_attempts", "retryable"),
        ("llm_provider_attempts", "error_classification"),
        ("llm_provider_attempts", "error_message"),
        ("llm_provider_attempts", "prepared_at"),
        ("llm_provider_attempts", "egress_started_at"),
        ("llm_provider_attempts", "completed_at"),
        ("agent_mutation_quota_reservations", "project_id"),
        ("agent_mutation_quota_reservations", "action_type"),
        ("agent_mutation_quota_reservations", "idempotency_key"),
        ("agent_mutation_quota_reservations", "policy_version"),
        ("agent_mutation_quota_reservations", "occurred_at"),
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
