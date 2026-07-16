-- Migration 004: Canonical PostgreSQL storage for the Agents service.
--
-- The obsolete 005_pgvector_setup.sql was parked under ClickHouse migrations
-- and described table shapes that the running Agents and Config services do
-- not use. This migration is deliberately aligned with the live Agents schema.
-- It does not create Config's experiments table or the unimplemented ui_configs
-- scaffold.

CREATE EXTENSION IF NOT EXISTS vector;

-- Preserve tables created by the obsolete manual migration before installing
-- the canonical shapes. Their incompatible UUID primary keys, vector(1536),
-- and column names cannot be altered in place without losing meaning. Backups
-- are intentionally never dropped automatically.
DO $reconcile_legacy_005$
BEGIN
    IF to_regclass('public.agent_memory') IS NOT NULL
       AND (
           EXISTS (
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agent_memory'
                 AND column_name = 'agent_type'
           )
           OR EXISTS (
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agent_memory'
                 AND column_name = 'id' AND data_type = 'uuid'
           )
       ) THEN
        IF to_regclass('public.agent_memory_legacy_005') IS NOT NULL THEN
            RAISE EXCEPTION 'Both agent_memory and agent_memory_legacy_005 contain legacy 005 data';
        END IF;
        ALTER TABLE public.agent_memory RENAME TO agent_memory_legacy_005;
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.agent_memory_legacy_005'::regclass
              AND conname = 'agent_memory_pkey'
        ) THEN
            ALTER TABLE public.agent_memory_legacy_005
                RENAME CONSTRAINT agent_memory_pkey TO agent_memory_legacy_005_pkey;
        END IF;
    END IF;

    IF to_regclass('public.agent_runs') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'agent_runs'
             AND column_name = 'run_id'
       )
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'agent_runs'
             AND column_name = 'id' AND data_type = 'uuid'
       ) THEN
        IF to_regclass('public.agent_runs_legacy_005') IS NOT NULL THEN
            RAISE EXCEPTION 'Both agent_runs and agent_runs_legacy_005 contain legacy 005 data';
        END IF;
        ALTER TABLE public.agent_runs RENAME TO agent_runs_legacy_005;
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.agent_runs_legacy_005'::regclass
              AND conname = 'agent_runs_pkey'
        ) THEN
            ALTER TABLE public.agent_runs_legacy_005
                RENAME CONSTRAINT agent_runs_pkey TO agent_runs_legacy_005_pkey;
        END IF;
    END IF;

    IF to_regclass('public.agent_audit_log') IS NOT NULL
       AND (
           EXISTS (
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agent_audit_log'
                 AND column_name = 'action_config'
           )
           OR EXISTS (
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'agent_audit_log'
                 AND column_name = 'id' AND data_type = 'uuid'
           )
       ) THEN
        IF to_regclass('public.agent_audit_log_legacy_005') IS NOT NULL THEN
            RAISE EXCEPTION 'Both agent_audit_log and agent_audit_log_legacy_005 contain legacy 005 data';
        END IF;
        ALTER TABLE public.agent_audit_log RENAME TO agent_audit_log_legacy_005;
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.agent_audit_log_legacy_005'::regclass
              AND conname = 'agent_audit_log_pkey'
        ) THEN
            ALTER TABLE public.agent_audit_log_legacy_005
                RENAME CONSTRAINT agent_audit_log_pkey TO agent_audit_log_legacy_005_pkey;
        END IF;
    END IF;

    IF to_regclass('public.experiments') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'experiments'
             AND column_name = 'experiment_key'
       ) THEN
        IF to_regclass('public.experiments_legacy_005') IS NOT NULL THEN
            RAISE EXCEPTION 'Both experiments and experiments_legacy_005 contain legacy 005 data';
        END IF;
        ALTER TABLE public.experiments RENAME TO experiments_legacy_005;
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.experiments_legacy_005'::regclass
              AND conname = 'experiments_pkey'
        ) THEN
            ALTER TABLE public.experiments_legacy_005
                RENAME CONSTRAINT experiments_pkey TO experiments_legacy_005_pkey;
        END IF;
    END IF;

    IF to_regclass('public.ui_configs') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'ui_configs'
             AND column_name = 'component_name'
       )
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'ui_configs'
             AND column_name = 'targeting_rules'
       ) THEN
        IF to_regclass('public.ui_configs_legacy_005') IS NOT NULL THEN
            RAISE EXCEPTION 'Both ui_configs and ui_configs_legacy_005 contain legacy 005 data';
        END IF;
        ALTER TABLE public.ui_configs RENAME TO ui_configs_legacy_005;
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.ui_configs_legacy_005'::regclass
              AND conname = 'ui_configs_pkey'
        ) THEN
            ALTER TABLE public.ui_configs_legacy_005
                RENAME CONSTRAINT ui_configs_pkey TO ui_configs_legacy_005_pkey;
        END IF;
    END IF;
END
$reconcile_legacy_005$;

CREATE TABLE IF NOT EXISTS agent_memory (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    embedding vector(384),
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Older live Agents builds could create this otherwise-compatible table with
-- a model-specific vector width. Preserve those embeddings (they cannot be
-- truthfully converted to another model's vector space), then install the
-- canonical vector(384) contract without silently discarding data.
DO $reconcile_agent_memory_dimension$
DECLARE
    current_dimension INTEGER;
BEGIN
    SELECT atttypmod INTO current_dimension
    FROM pg_attribute
    WHERE attrelid = 'public.agent_memory'::regclass
      AND attname = 'embedding' AND NOT attisdropped;

    IF current_dimension IS DISTINCT FROM 384 THEN
        IF to_regclass('public.agent_memory_legacy_vectors') IS NOT NULL THEN
            RAISE EXCEPTION
                'agent_memory_legacy_vectors already exists during vector reconciliation';
        END IF;
        CREATE TABLE agent_memory_legacy_vectors AS
            SELECT * FROM agent_memory;
        DROP INDEX IF EXISTS idx_agent_memory_embedding;
        DELETE FROM agent_memory;
        ALTER TABLE agent_memory ALTER COLUMN embedding TYPE vector(384);
    END IF;
END
$reconcile_agent_memory_dimension$;

CREATE INDEX IF NOT EXISTS idx_agent_memory_project
    ON agent_memory (project_id);
CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding
    ON agent_memory USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    autonomy_level INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'started',
    phase TEXT DEFAULT 'initializing',
    insights_count INTEGER DEFAULT 0,
    experiments_count INTEGER DEFAULT 0,
    config JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    lease_owner_id TEXT,
    lease_expires_at TIMESTAMPTZ
);

-- Databases booted by an older Agents replica may already have agent_runs.
-- Preserve NULL as the only default so an old replica cannot create a row
-- that looks leased by a new worker.
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS lease_owner_id TEXT;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE agent_runs
    ALTER COLUMN lease_expires_at DROP DEFAULT;

CREATE INDEX IF NOT EXISTS idx_agent_runs_project_started
    ON agent_runs (project_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status
    ON agent_runs (status, phase);
CREATE INDEX IF NOT EXISTS idx_agent_runs_lease_expiry
    ON agent_runs (lease_expires_at)
    WHERE status IN ('started', 'running')
       OR (phase = 'resuming' AND status IN ('approved', 'rejected'));

CREATE TABLE IF NOT EXISTS agent_audit_log (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    config JSONB DEFAULT '{}',
    safety_result JSONB DEFAULT '{}',
    approval_status TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_audit_run_created
    ON agent_audit_log (run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_run_results (
    run_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    produces TEXT NOT NULL,
    output JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (run_id, agent_name)
);

CREATE TABLE IF NOT EXISTS feature_proposals (
    proposal_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT,
    claim_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'approved',
    title TEXT NOT NULL,
    spec TEXT NOT NULL,
    priority TEXT,
    changeset_id TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE feature_proposals
    ADD COLUMN IF NOT EXISTS claim_run_id TEXT;

CREATE INDEX IF NOT EXISTS feature_proposals_project_status_idx
    ON feature_proposals (project_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_feature_proposals_claim_run
    ON feature_proposals (claim_run_id)
    WHERE status = 'implementing';

-- Bind an unambiguous in-flight proposal from an older replica to its run so
-- lease recovery reopens only work abandoned by that exact run.
WITH active_claims AS (
    SELECT project_id,
           config ->> 'target_proposal_id' AS proposal_id,
           min(run_id) AS claim_run_id
    FROM agent_runs
    WHERE config ->> 'target_proposal_id' IS NOT NULL
      AND (
          status IN ('started', 'running', 'waiting_approval')
          OR (phase = 'resuming' AND status IN ('approved', 'rejected'))
      )
    GROUP BY project_id, config ->> 'target_proposal_id'
    HAVING count(*) = 1
)
UPDATE feature_proposals AS proposal
SET claim_run_id = active_claims.claim_run_id,
    updated_at = now()
FROM active_claims
WHERE proposal.project_id = active_claims.project_id
  AND proposal.proposal_id = active_claims.proposal_id
  AND proposal.status = 'implementing'
  AND proposal.claim_run_id IS NULL;

CREATE TABLE IF NOT EXISTS designed_experiments (
    project_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    run_id TEXT,
    insight_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    hypothesis TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'designed',
    changeset_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, experiment_id)
);

ALTER TABLE designed_experiments
    ADD COLUMN IF NOT EXISTS changeset_id TEXT;
CREATE INDEX IF NOT EXISTS designed_experiments_project_created_idx
    ON designed_experiments (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS experiment_verdicts (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    run_id TEXT,
    verdict TEXT NOT NULL,
    reasoning TEXT NOT NULL DEFAULT '',
    results JSONB DEFAULT '{}',
    durable_feature TEXT NOT NULL DEFAULT '',
    consumed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS experiment_verdicts_project_created_idx
    ON experiment_verdicts (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS custom_agents (
    agent_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL,
    user_prompt_template TEXT NOT NULL,
    model_tier TEXT NOT NULL DEFAULT 'reasoning'
        CHECK (model_tier IN ('fast', 'reasoning')),
    tools JSONB NOT NULL DEFAULT '[]',
    preset_tools JSONB NOT NULL DEFAULT '[]',
    requires JSONB NOT NULL DEFAULT '[]',
    produces TEXT NOT NULL,
    memory_query TEXT,
    memory_top_k INTEGER NOT NULL DEFAULT 5,
    pipeline_order INTEGER NOT NULL DEFAULT 100,
    max_tool_steps INTEGER NOT NULL DEFAULT 8,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE custom_agents
    ADD COLUMN IF NOT EXISTS max_tool_steps INTEGER NOT NULL DEFAULT 8;
ALTER TABLE custom_agents
    ADD COLUMN IF NOT EXISTS preset_tools JSONB NOT NULL DEFAULT '[]';
CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_agents_project_slug
    ON custom_agents (project_id, slug) WHERE status = 'active';
