"""Fail-fast validation for the Codegen service's migrated schema."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MIGRATION_VERSION = 55
MIGRATION_NAME = "055_codegen_tenant_publication.sql"
REQUIRED_COLUMNS = frozenset(
    {
        ("admin_project_execution_authorizations", "project_id"),
        ("admin_project_execution_authorizations", "authorization_source"),
        ("admin_project_execution_authorizations", "actor"),
        ("admin_project_execution_authorizations", "reason"),
        ("admin_project_execution_authorizations", "authorized_at"),
        ("codegen_connections", "project_id"),
        ("codegen_connections", "grant_id"),
        ("codegen_connections", "default_base_branch"),
        ("codegen_connections", "tenant_policy"),
        ("codegen_connections_legacy_unverified", "project_id"),
        ("codegen_connections_legacy_unverified", "installation_id"),
        ("codegen_connections_legacy_unverified", "repo"),
        ("codegen_connections_legacy_unverified", "quarantined_at"),
        ("github_repository_grants", "grant_id"),
        ("github_repository_grants", "project_id"),
        ("github_repository_grants", "installation_id"),
        ("github_repository_grants", "repository_id"),
        ("github_repository_grants", "repository_full_name"),
        ("github_repository_grants", "status"),
        ("github_repository_grants", "authorization_source"),
        ("github_repository_grants", "authorization_subject"),
        ("github_repository_grants", "verified_at"),
        ("github_repository_grants", "revoked_at"),
        ("codegen_changesets", "changeset_id"),
        ("codegen_changesets", "project_id"),
        ("codegen_changesets", "idempotency_key"),
        ("codegen_changesets", "idempotency_request_sha256"),
        ("codegen_changesets", "repository_grant_id"),
        ("codegen_changesets", "repository_id"),
        ("codegen_changesets", "repository_installation_id"),
        ("codegen_changesets", "repository_full_name"),
        ("codegen_changesets", "repository_target_quarantined"),
        ("codegen_changesets", "status"),
        ("codegen_changesets", "head_sha"),
        ("codegen_changesets", "github_pr_status"),
        ("codegen_changesets", "external_ci_status"),
        ("codegen_changesets", "merge_sha"),
        ("codegen_changesets", "prompts"),
        ("codegen_changesets", "contract_bundle"),
        ("codegen_changesets", "requirement_ledger"),
        ("codegen_changesets", "inspection_snapshot"),
        ("codegen_changesets", "dependency_slice"),
        ("codegen_changesets", "verification_plan"),
        ("codegen_changesets", "verification_coverage"),
        ("codegen_changesets", "runtime_acceptance_plan"),
        ("codegen_changesets", "runtime_evidence_assessment"),
        ("codegen_changesets", "review_verdict"),
        ("codegen_changesets", "publication_authorization"),
        ("codegen_changesets", "publication_authorization_legacy"),
        (
            "codegen_changesets",
            "publication_authorization_segmentless_legacy",
        ),
        (
            "codegen_changesets",
            "publication_authorization_egress_unattested_legacy",
        ),
        (
            "codegen_changesets",
            "publication_authorization_pre_tenant_legacy",
        ),
        ("codegen_changesets", "tenant_policy_snapshot"),
        ("codegen_changesets", "effective_safety_policy_sha256"),
        ("codegen_changesets", "external_ci_awaiting_since"),
        ("codegen_changesets", "ci_retry_count"),
        ("codegen_changesets", "ci_remediation_status"),
        ("codegen_changesets", "retry_of_changeset_id"),
        ("codegen_changesets", "control_metadata"),
        ("codegen_changesets", "llm_execution_snapshot"),
        ("codegen_changesets", "llm_execution_snapshot_v1_legacy"),
        ("codegen_project_provider_credentials", "credential_id"),
        ("codegen_project_provider_credentials", "project_id"),
        ("codegen_project_provider_credentials", "provider"),
        ("codegen_project_provider_credentials", "credential_version"),
        ("codegen_project_provider_credentials", "state"),
        ("codegen_project_provider_credentials", "ciphertext"),
        ("codegen_project_provider_credentials", "nonce"),
        ("codegen_project_provider_credentials", "encryption_key_id"),
        ("codegen_project_provider_connections", "project_id"),
        ("codegen_project_provider_connections", "provider"),
        ("codegen_project_provider_connections", "version"),
        ("codegen_project_provider_connections", "inventory_version"),
        ("codegen_project_provider_connections", "state"),
        ("codegen_project_provider_connections", "credential_id"),
        ("codegen_project_provider_models", "project_id"),
        ("codegen_project_provider_models", "provider"),
        ("codegen_project_provider_models", "model_id"),
        ("codegen_project_provider_models", "supported_roles"),
        ("codegen_project_model_assignments", "project_id"),
        ("codegen_project_model_assignments", "role"),
        ("codegen_project_model_assignments", "provider"),
        ("codegen_project_model_assignments", "model_id"),
        ("codegen_project_model_assignments", "assignment_version"),
        ("codegen_llm_attempts", "attempt_id"),
        ("codegen_llm_attempts", "project_id"),
        ("codegen_llm_attempts", "changeset_id"),
        ("codegen_llm_attempts", "phase"),
        ("codegen_llm_attempts", "role"),
        ("codegen_llm_attempts", "credential_id"),
        ("codegen_pull_request_observations", "github_updated_at"),
        ("codegen_pull_request_publication_events", "event_id"),
        ("codegen_pull_request_publication_events", "event_sequence"),
        ("codegen_pull_request_publication_events", "changeset_id"),
        ("codegen_pull_request_publication_events", "event_type"),
        ("codegen_pull_request_publication_events", "intent_event_id"),
        ("codegen_pull_request_publication_events", "cleanup_request_event_id"),
        ("codegen_pull_request_publication_events", "pr_number"),
        ("codegen_pull_request_publication_events", "github_url"),
        ("codegen_pull_request_publication_events", "recorded_at"),
        ("codegen_pull_request_publication_events", "inserted_at"),
        ("codegen_pull_request_publication_events", "payload"),
        ("codegen_ci_verification_observations", "evidence_hash"),
        ("codegen_runtime_evidence_observations", "ci_observation_id"),
        ("codegen_runtime_collection_claims", "ci_observation_id"),
        ("codegen_ci_remediation_attempts", "failure_observation_id"),
        ("codegen_ci_remediation_claims", "failure_observation_id"),
    }
)


async def assert_schema_ready(conn: Any) -> None:
    """Reject startup unless the checksummed Codegen migration is present."""
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
            f"Codegen PostgreSQL schema is incomplete ({formatted}); "
            "restore the database or apply migrations before startup"
        )
