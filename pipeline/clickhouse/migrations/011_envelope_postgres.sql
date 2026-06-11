-- Migration 011: Align Postgres tables to the canonical envelope, add llm_calls.
-- Target: PostgreSQL
-- Apply with: psql $POSTGRES_URL < 011_envelope_postgres.sql
--
-- Three things happen here:
--   1. agent_audit_log gets envelope columns. It remains the source of truth
--      for the *mutable* approval lifecycle (pending -> approved -> rejected).
--      Once approval_status flips to approved/auto, a mirror row is written
--      to ClickHouse decisions_v2 for analytics.
--   2. agent_memory gets a documented metadata convention (no schema change
--      required — the JSONB column already exists — but a CHECK constraint
--      enforces the envelope keys when present).
--   3. llm_calls is added to track every LLM invocation: model, tokens,
--      latency, and pointers to the raw prompt/completion blobs in object
--      storage (rows stay small; blobs go to S3).

-- ---------- 1. envelope columns on agent_audit_log ----------
ALTER TABLE agent_audit_log
    ADD COLUMN IF NOT EXISTS schema_version  TEXT NOT NULL DEFAULT 'agent_action@1',
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT,
    ADD COLUMN IF NOT EXISTS correlation_id  UUID,
    ADD COLUMN IF NOT EXISTS source          TEXT NOT NULL DEFAULT 'agents-service@1',
    ADD COLUMN IF NOT EXISTS occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now();

-- Idempotency keys must be unique per project when present, so retries of the
-- same logical action (same run_id, action_type, config-hash) collapse.
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_idem
    ON agent_audit_log(project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_correlation
    ON agent_audit_log(correlation_id)
    WHERE correlation_id IS NOT NULL;

-- ---------- 2. agent_memory: envelope-aware metadata ----------
-- The metadata JSONB column already exists. We don't reshape it — instead we
-- *recommend* (via a CHECK that only fires when keys are present) the
-- envelope convention so embeddings carry traceable provenance.
-- Postgres has no ADD CONSTRAINT IF NOT EXISTS, so we guard with a DO block
-- that checks pg_constraint first. Re-running the migration is a no-op.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_memory_envelope'
    ) THEN
        ALTER TABLE agent_memory
            ADD CONSTRAINT chk_memory_envelope CHECK (
                (NOT (metadata ? '_schema')         OR jsonb_typeof(metadata->'_schema')         = 'string') AND
                (NOT (metadata ? '_correlation_id') OR jsonb_typeof(metadata->'_correlation_id') = 'string') AND
                (NOT (metadata ? '_source')         OR jsonb_typeof(metadata->'_source')         = 'string')
            );
    END IF;
END $$;

-- ---------- 3. llm_calls ----------
-- Small fixed schema per LLM call. Raw prompt + completion live in object
-- storage; this row only holds the SHA-256 pointer + tiny stats.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- envelope
    schema_version     TEXT NOT NULL DEFAULT 'llm_call@1',
    project_id         INTEGER NOT NULL,
    run_id             UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    correlation_id     UUID,
    source             TEXT NOT NULL,                      -- e.g. 'agents.supervisor@1.2.3'
    occurred_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- call shape
    provider           TEXT NOT NULL,                      -- 'anthropic' | 'openai' | 'google' | 'local'
    model              TEXT NOT NULL,                      -- 'claude-opus-4-6' etc.
    purpose            TEXT NOT NULL,                      -- 'analyze_behavior' | 'design_experiment' | ...

    -- cost / perf
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER NOT NULL DEFAULT 0,
    cost_usd_micros    BIGINT  NOT NULL DEFAULT 0,         -- $0.000001 units, integer-safe

    -- result / error
    status             TEXT NOT NULL DEFAULT 'ok',          -- 'ok' | 'error' | 'safety_block'
    error_message      TEXT,

    -- blob pointers (object storage, content-addressed by SHA-256)
    prompt_sha256      CHAR(64),
    prompt_uri         TEXT,                                -- e.g. 's3://apdl-llm/{project}/prompts/{sha256}.json'
    completion_sha256  CHAR(64),
    completion_uri     TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_run        ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_project    ON llm_calls(project_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_corr       ON llm_calls(correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_calls_model      ON llm_calls(provider, model);
