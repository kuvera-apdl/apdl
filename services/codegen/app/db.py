"""Schema DDL for the codegen service.

Tables live in the shared APDL PostgreSQL database (no pgvector needed) and are
created idempotently on startup, mirroring the agents service convention
(``services/agents/app/main.py``).
"""

from __future__ import annotations

CONNECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS codegen_connections (
    project_id          TEXT PRIMARY KEY,
    installation_id     BIGINT NOT NULL,
    repo                TEXT NOT NULL,
    default_base_branch TEXT NOT NULL DEFAULT 'main',
    policy              JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CHANGESETS_DDL = """
CREATE TABLE IF NOT EXISTS codegen_changesets (
    changeset_id  TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    run_id        TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    base_branch   TEXT,
    branch        TEXT,
    pr_url        TEXT,
    pr_number     INTEGER,
    pr_node_id    TEXT,
    ci_status     TEXT,
    merge_sha     TEXT,
    task          JSONB NOT NULL DEFAULT '{}',
    diff_stat     JSONB NOT NULL DEFAULT '{}',
    prompts       JSONB NOT NULL DEFAULT '[]',
    contract_bundle JSONB,
    requirement_ledger JSONB,
    inspection_snapshot JSONB,
    dependency_slice JSONB,
    verification_plan JSONB,
    verification_coverage JSONB,
    ci_awaiting_since TIMESTAMPTZ,
    ci_retry_count INTEGER NOT NULL DEFAULT 0,
    ci_remediation_status TEXT NOT NULL DEFAULT 'idle',
    ci_failure_key TEXT,
    ci_failure_summary TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

CHANGESETS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_codegen_changesets_project
ON codegen_changesets (project_id, created_at DESC);
"""

# Additive migration for deployments whose table predates the column (the
# CREATE above is IF NOT EXISTS, so it never alters an existing table).
CHANGESETS_MERGE_SHA_DDL = """
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS merge_sha TEXT;
"""

CHANGESETS_PROMPTS_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS prompts JSONB NOT NULL DEFAULT '[]';
"""

CHANGESETS_CONTRACT_BUNDLE_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS contract_bundle JSONB;
"""

CHANGESETS_REQUIREMENT_LEDGER_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS requirement_ledger JSONB;
"""

CHANGESETS_INSPECTION_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS inspection_snapshot JSONB;
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS dependency_slice JSONB;
"""

CHANGESETS_VERIFICATION_PLAN_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS verification_plan JSONB;
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS verification_coverage JSONB;
"""

# When the changeset started awaiting CI (set once at pr_open). The CI sync's
# grace window and pending deadline anchor on this rather than updated_at,
# which every status transition refreshes — the sync must not reset its own
# clock. Nullable: rows that predate the column fall back to updated_at.
CHANGESETS_CI_AWAITING_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS ci_awaiting_since TIMESTAMPTZ;
"""

CHANGESETS_CI_REMEDIATION_DDL = """
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS ci_retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS ci_remediation_status TEXT NOT NULL DEFAULT 'idle';
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS ci_failure_key TEXT;
ALTER TABLE codegen_changesets
ADD COLUMN IF NOT EXISTS ci_failure_summary TEXT;
UPDATE codegen_changesets
SET status = CASE
        WHEN status IN ('pr_open', 'ci_running', 'ci_passed', 'waiting_approval')
        THEN 'unverified_external_ci'
        ELSE status
    END,
    ci_status = 'unverified_external_ci'
WHERE ci_status IN ('none', 'no_report');
"""

ALL_DDL = (
    CONNECTIONS_DDL,
    CHANGESETS_DDL,
    CHANGESETS_INDEX_DDL,
    CHANGESETS_MERGE_SHA_DDL,
    CHANGESETS_PROMPTS_DDL,
    CHANGESETS_CONTRACT_BUNDLE_DDL,
    CHANGESETS_REQUIREMENT_LEDGER_DDL,
    CHANGESETS_INSPECTION_DDL,
    CHANGESETS_VERIFICATION_PLAN_DDL,
    CHANGESETS_CI_AWAITING_DDL,
    CHANGESETS_CI_REMEDIATION_DDL,
)
