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

ALL_DDL = (
    CONNECTIONS_DDL,
    CHANGESETS_DDL,
    CHANGESETS_INDEX_DDL,
    CHANGESETS_MERGE_SHA_DDL,
    CHANGESETS_PROMPTS_DDL,
)
