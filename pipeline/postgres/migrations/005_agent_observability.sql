-- Migration 005: Envelope metadata and LLM-call observability for Agents.
--
-- This is the compatible replacement for the obsolete PostgreSQL migration
-- 011_envelope_postgres.sql. Agent run identifiers and project identifiers are
-- TEXT in the running services, so this migration preserves those contracts.

-- Preserve an llm_calls table created from the incompatible old migration.
DO $reconcile_legacy_011$
BEGIN
    IF to_regclass('public.llm_calls') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = 'llm_calls'
             AND column_name = 'project_id' AND data_type = 'integer'
       ) THEN
        IF to_regclass('public.llm_calls_legacy_011') IS NOT NULL THEN
            RAISE EXCEPTION 'Both llm_calls and llm_calls_legacy_011 contain legacy 011 data';
        END IF;
        ALTER TABLE public.llm_calls RENAME TO llm_calls_legacy_011;
        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'public.llm_calls_legacy_011'::regclass
              AND conname = 'llm_calls_pkey'
        ) THEN
            ALTER TABLE public.llm_calls_legacy_011
                RENAME CONSTRAINT llm_calls_pkey TO llm_calls_legacy_011_pkey;
        END IF;
    END IF;
END
$reconcile_legacy_011$;

ALTER TABLE agent_audit_log
    ADD COLUMN IF NOT EXISTS schema_version TEXT NOT NULL DEFAULT 'agent_action@1',
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS correlation_id UUID,
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'agents-service@1',
    ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ;

UPDATE agent_audit_log
SET occurred_at = created_at
WHERE occurred_at IS NULL;

ALTER TABLE agent_audit_log
    ALTER COLUMN occurred_at SET DEFAULT now(),
    ALTER COLUMN occurred_at SET NOT NULL;

-- A run belongs to exactly one project, so run_id is the compatible tenant
-- scope for audit idempotency without duplicating project_id on every row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_audit_idempotency
    ON agent_audit_log (run_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_audit_correlation
    ON agent_audit_log (correlation_id)
    WHERE correlation_id IS NOT NULL;

DO $agent_memory_envelope$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.agent_memory'::regclass
          AND conname = 'chk_agent_memory_envelope'
    ) THEN
        ALTER TABLE agent_memory
            ADD CONSTRAINT chk_agent_memory_envelope CHECK (
                (NOT (metadata ? '_schema')
                    OR jsonb_typeof(metadata -> '_schema') = 'string')
                AND (NOT (metadata ? '_correlation_id')
                    OR jsonb_typeof(metadata -> '_correlation_id') = 'string')
                AND (NOT (metadata ? '_source')
                    OR jsonb_typeof(metadata -> '_source') = 'string')
            );
    END IF;
END
$agent_memory_envelope$;

CREATE TABLE IF NOT EXISTS llm_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_version TEXT NOT NULL DEFAULT 'llm_call@1',
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    correlation_id UUID,
    source TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    cost_usd_micros BIGINT NOT NULL DEFAULT 0 CHECK (cost_usd_micros >= 0),
    status TEXT NOT NULL DEFAULT 'ok'
        CHECK (status IN ('ok', 'error', 'safety_block')),
    error_message TEXT,
    prompt_sha256 CHAR(64)
        CHECK (prompt_sha256 IS NULL OR prompt_sha256 ~ '^[0-9a-f]{64}$'),
    prompt_uri TEXT,
    completion_sha256 CHAR(64)
        CHECK (completion_sha256 IS NULL OR completion_sha256 ~ '^[0-9a-f]{64}$'),
    completion_uri TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_llm_calls_run
    ON llm_calls (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_llm_calls_project
    ON llm_calls (project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_llm_calls_correlation
    ON llm_calls (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_agent_llm_calls_model
    ON llm_calls (provider, model);
