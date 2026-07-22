-- Migration 034: one database-authoritative execution lane per Agents project.
--
-- A run owns its project's lane from its initial queue insert through execution,
-- approval waiting, approval effects, and approval resume.  Only an explicit
-- terminal status releases the lane.  The generated column is the canonical
-- predicate and the unique index is the cross-replica concurrency authority;
-- application advisory locks are only a friendly early-conflict check.

ALTER TABLE agent_runs
    ADD CONSTRAINT agent_runs_status_check
    CHECK (
        status IN (
            'started',
            'running',
            'waiting_approval',
            'approval_queued',
            'cancelling',
            'approved',
            'rejected',
            'completed',
            'completed_with_errors',
            'failed',
            'cancelled',
            'manual_intervention'
        )
    ) NOT VALID;

ALTER TABLE agent_runs
    ADD COLUMN execution_lane_project_id TEXT
    GENERATED ALWAYS AS (
        CASE
            WHEN status IN (
                'completed',
                'completed_with_errors',
                'failed',
                'cancelled',
                'manual_intervention'
            ) THEN NULL
            ELSE project_id
        END
    ) STORED;

DO $validate_existing_execution_lanes$
DECLARE
    conflicting_project_id TEXT;
    conflicting_run_ids TEXT[];
BEGIN
    SELECT run.project_id, array_agg(run.run_id ORDER BY run.run_id)
    INTO conflicting_project_id, conflicting_run_ids
    FROM agent_runs AS run
    WHERE run.execution_lane_project_id IS NOT NULL
    GROUP BY run.project_id
    HAVING count(*) > 1
    ORDER BY run.project_id
    LIMIT 1;

    IF conflicting_project_id IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23505',
            MESSAGE = format(
                'project %s has multiple non-terminal agent runs: %s',
                conflicting_project_id,
                conflicting_run_ids
            ),
            HINT = 'Terminalize all but one run before applying migration 034.';
    END IF;
END
$validate_existing_execution_lanes$;

DO $validate_existing_approval_effect_lanes$
DECLARE
    invalid_run_id TEXT;
    invalid_project_id TEXT;
    invalid_effect_id UUID;
BEGIN
    SELECT run.run_id, run.project_id, effect.effect_id
    INTO invalid_run_id, invalid_project_id, invalid_effect_id
    FROM agent_runs AS run
    JOIN agent_approval_effects AS effect
      ON effect.run_id = run.run_id
     AND effect.project_id = run.project_id
    WHERE run.execution_lane_project_id IS NULL
      AND effect.status IN ('queued', 'processing', 'retryable_failed')
    ORDER BY run.run_id, effect.effect_id
    LIMIT 1;

    IF invalid_run_id IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'terminal agent run %s for project %s has live approval effect %s',
                invalid_run_id,
                invalid_project_id,
                invalid_effect_id
            ),
            HINT = 'Reconcile the approval effect or restore the run to a lane-owning status before applying migration 034.';
    END IF;
END
$validate_existing_approval_effect_lanes$;

ALTER TABLE agent_runs
    VALIDATE CONSTRAINT agent_runs_status_check;

CREATE UNIQUE INDEX agent_runs_one_execution_lane_per_project_idx
    ON agent_runs (execution_lane_project_id)
    WHERE execution_lane_project_id IS NOT NULL;

COMMENT ON COLUMN agent_runs.execution_lane_project_id IS
    'Database-generated per-project execution lane; NULL only for terminal runs.';

-- The status-generated lane must not be releasable while a durable approval
-- effect can still mutate Config or Codegen.  Application transactions lock the
-- run before claiming or settling an effect; this trigger is the final database
-- authority if any caller attempts to bypass that state machine.
CREATE OR REPLACE FUNCTION apdl_guard_agent_execution_lane_release()
RETURNS trigger
LANGUAGE plpgsql
AS $guard_agent_execution_lane_release$
BEGIN
    IF NEW.status IN (
        'completed',
        'completed_with_errors',
        'failed',
        'cancelled',
        'manual_intervention'
    ) AND EXISTS (
        SELECT 1
        FROM agent_approval_effects AS effect
        WHERE effect.run_id = OLD.run_id
          AND effect.project_id = OLD.project_id
          AND effect.status IN ('queued', 'processing', 'retryable_failed')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'agent run %s cannot release project %s execution lane while approval effects are live',
                OLD.run_id,
                OLD.project_id
            ),
            HINT = 'Drain or reconcile every approval effect before terminalizing the run.';
    END IF;
    RETURN NEW;
END
$guard_agent_execution_lane_release$;

CREATE TRIGGER agent_runs_guard_execution_lane_release
BEFORE UPDATE OF status ON agent_runs
FOR EACH ROW
WHEN (OLD.status IS DISTINCT FROM NEW.status)
EXECUTE FUNCTION apdl_guard_agent_execution_lane_release();

-- Enforce the reverse invariant too.  Taking the run row lock before a live
-- effect is inserted or reactivated serializes this check with the run-status
-- trigger above, so concurrent cross-table writes cannot create a lane-null
-- run with executable approval work.
CREATE OR REPLACE FUNCTION apdl_guard_agent_live_effect_lane()
RETURNS trigger
LANGUAGE plpgsql
AS $guard_agent_live_effect_lane$
DECLARE
    lane_project_id TEXT;
BEGIN
    SELECT run.execution_lane_project_id
    INTO lane_project_id
    FROM agent_runs AS run
    WHERE run.run_id = NEW.run_id
      AND run.project_id = NEW.project_id
    FOR UPDATE;

    IF NOT FOUND OR lane_project_id IS DISTINCT FROM NEW.project_id THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'approval effect %s cannot become live without an active project execution lane',
                NEW.effect_id
            ),
            HINT = 'Create or restore the owning run lane before queuing approval work.';
    END IF;
    RETURN NEW;
END
$guard_agent_live_effect_lane$;

CREATE TRIGGER agent_approval_effects_guard_live_lane_insert
BEFORE INSERT ON agent_approval_effects
FOR EACH ROW
WHEN (NEW.status IN ('queued', 'processing', 'retryable_failed'))
EXECUTE FUNCTION apdl_guard_agent_live_effect_lane();

CREATE TRIGGER agent_approval_effects_guard_live_lane_update
BEFORE UPDATE OF status, run_id, project_id ON agent_approval_effects
FOR EACH ROW
WHEN (NEW.status IN ('queued', 'processing', 'retryable_failed'))
EXECUTE FUNCTION apdl_guard_agent_live_effect_lane();
