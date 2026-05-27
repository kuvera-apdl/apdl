-- Migration 005: PostgreSQL pgvector setup for agent memory
-- NOTE: This runs against PostgreSQL, NOT ClickHouse
-- Execute with: psql $POSTGRES_URL < 005_pgvector_setup.sql

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS agent_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    embedding vector(1536) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_project ON agent_memory(project_id);
CREATE INDEX IF NOT EXISTS idx_memory_agent_type ON agent_memory(project_id, agent_type);
CREATE INDEX IF NOT EXISTS idx_memory_embedding ON agent_memory
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Audit trail table for agent actions
CREATE TABLE IF NOT EXISTS agent_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_config JSONB NOT NULL DEFAULT '{}',
    safety_result JSONB,
    approval_status TEXT DEFAULT 'pending',
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_run ON agent_audit_log(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON agent_audit_log(project_id);

-- Agent run tracking table
CREATE TABLE IF NOT EXISTS agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    autonomy_level INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'running',
    phase TEXT DEFAULT 'starting',
    insights_count INTEGER DEFAULT 0,
    experiments_count INTEGER DEFAULT 0,
    personalizations_count INTEGER DEFAULT 0,
    proposals_count INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON agent_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON agent_runs(status);

-- Feature flags table (used by Config Service)
CREATE TABLE IF NOT EXISTS feature_flags (
    id SERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    flag_key TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    enabled BOOLEAN DEFAULT false,
    salt TEXT NOT NULL,
    rollout_percentage INTEGER DEFAULT 0,
    rules JSONB DEFAULT '[]',
    variants JSONB DEFAULT '[]',
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(project_id, flag_key)
);

CREATE INDEX IF NOT EXISTS idx_flags_project ON feature_flags(project_id);

-- Experiments table
CREATE TABLE IF NOT EXISTS experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL,
    experiment_key TEXT NOT NULL,
    name TEXT NOT NULL,
    hypothesis TEXT DEFAULT '',
    flag_key TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    variants JSONB DEFAULT '[]',
    target_metrics JSONB DEFAULT '[]',
    guardrail_metrics JSONB DEFAULT '[]',
    targeting_rules JSONB DEFAULT '[]',
    sample_size INTEGER DEFAULT 0,
    start_date TIMESTAMPTZ,
    end_date TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(project_id, experiment_key)
);

CREATE INDEX IF NOT EXISTS idx_experiments_project ON experiments(project_id);
CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(project_id, status);

-- UI configurations table
CREATE TABLE IF NOT EXISTS ui_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    component_name TEXT NOT NULL,
    props JSONB DEFAULT '{}',
    targeting_rules JSONB DEFAULT '[]',
    experiment_id UUID,
    variant TEXT,
    priority INTEGER DEFAULT 0,
    enabled BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ui_configs_project ON ui_configs(project_id);
CREATE INDEX IF NOT EXISTS idx_ui_configs_slot ON ui_configs(project_id, slot_id);
