-- Make the custom-agent tool contract exact. Before this migration an empty
-- tools array meant "the whole catalog"; preserve that meaning for existing
-- rows by materializing the catalog that existed at the contract boundary.
-- New rows may then use [] to mean that the model has no agentic tools.
UPDATE custom_agents
SET tools = '["discover_events", "query_events", "query_timeseries", "query_funnel", "query_retention", "query_cohort", "query_breakdown", "list_flags", "get_active_experiments"]'::jsonb,
    updated_at = now()
WHERE tools = '[]'::jsonb;

-- Custom-agent output is canonically list-shaped. Some pre-canonical
-- databases carried a parse_as column; keeping it would create a second,
-- contradictory contract that current code never reads.
ALTER TABLE custom_agents
    DROP COLUMN IF EXISTS parse_as;

-- Dry-runs execute real warehouse queries and LLM calls. Keep a durable,
-- project-scoped cost/audit record and a lease that provides a replica-safe
-- single-flight boundary. A crashed replica's lease is terminalized by the
-- next claimant after lease_expires_at.
CREATE TABLE custom_agent_test_runs (
    test_run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    agent_slug TEXT NOT NULL,
    model_tier TEXT NOT NULL,
    time_range_days INTEGER NOT NULL,
    max_tool_steps INTEGER NOT NULL,
    allowed_tool_count INTEGER NOT NULL,
    configured_preset_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    preset_tool_calls INTEGER NOT NULL DEFAULT 0,
    agentic_tool_calls INTEGER NOT NULL DEFAULT 0,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    llm_latency_ms INTEGER,
    total_latency_ms INTEGER,
    error TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT custom_agent_test_runs_status_check
        CHECK (status IN ('running', 'succeeded', 'failed')),
    CONSTRAINT custom_agent_test_runs_time_range_check
        CHECK (time_range_days BETWEEN 1 AND 90),
    CONSTRAINT custom_agent_test_runs_tool_steps_check
        CHECK (max_tool_steps BETWEEN 1 AND 16),
    CONSTRAINT custom_agent_test_runs_counts_check
        CHECK (
            allowed_tool_count >= 0
            AND configured_preset_count >= 0
            AND preset_tool_calls >= 0
            AND agentic_tool_calls >= 0
            AND llm_calls >= 0
        )
);

CREATE UNIQUE INDEX custom_agent_test_runs_one_running_per_project_idx
    ON custom_agent_test_runs (project_id)
    WHERE status = 'running';

CREATE INDEX custom_agent_test_runs_project_started_idx
    ON custom_agent_test_runs (project_id, started_at DESC);

CREATE INDEX custom_agent_test_runs_expired_lease_idx
    ON custom_agent_test_runs (lease_expires_at)
    WHERE status = 'running';

-- Retry lineage is a first-class column, not an inference from mutable JSON.
-- Preserve legacy children by selecting one deterministic canonical child per
-- parent; any pre-existing duplicate rows remain historical records but cannot
-- become the target returned by a new idempotent retry request.
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS retry_of_changeset_id TEXT;

WITH legacy_retry_children AS (
    SELECT child.changeset_id,
           parent.changeset_id AS retry_of_changeset_id,
           row_number() OVER (
               PARTITION BY parent.changeset_id
               ORDER BY child.created_at, child.changeset_id
           ) AS retry_rank
    FROM codegen_changesets AS child
    JOIN codegen_changesets AS parent
      ON parent.changeset_id = (child.task -> 'context' ->> 'retry_of')
    WHERE child.retry_of_changeset_id IS NULL
      AND child.changeset_id <> parent.changeset_id
)
UPDATE codegen_changesets AS child
SET retry_of_changeset_id = legacy.retry_of_changeset_id
FROM legacy_retry_children AS legacy
WHERE child.changeset_id = legacy.changeset_id
  AND legacy.retry_rank = 1;

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_retry_of_changeset_id_fkey;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_retry_of_changeset_id_fkey
    FOREIGN KEY (retry_of_changeset_id)
    REFERENCES codegen_changesets(changeset_id)
    ON DELETE RESTRICT;

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_retry_not_self_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_retry_not_self_check
    CHECK (retry_of_changeset_id IS NULL OR retry_of_changeset_id <> changeset_id);

CREATE UNIQUE INDEX codegen_changesets_one_retry_child_idx
    ON codegen_changesets (retry_of_changeset_id)
    WHERE retry_of_changeset_id IS NOT NULL;
