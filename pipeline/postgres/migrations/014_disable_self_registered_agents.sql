-- Self-registration provisions the core analytics product, but it does not
-- provision the operator-controlled resources required for Agents execution.
-- Keep project provenance durable so deleting an admin user cannot silently
-- turn a self-registered project into an operator-provisioned project.
ALTER TABLE admin_projects
    DROP CONSTRAINT IF EXISTS admin_projects_created_by_fkey;
ALTER TABLE admin_projects
    ADD CONSTRAINT admin_projects_created_by_fkey
    FOREIGN KEY (created_by) REFERENCES admin_users(user_id) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION reject_admin_project_creator_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $reject_admin_project_creator_change$
BEGIN
    IF NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'admin_projects creator provenance is immutable';
    END IF;
    RETURN NEW;
END
$reject_admin_project_creator_change$;

DROP TRIGGER IF EXISTS admin_projects_creator_immutable ON admin_projects;
CREATE TRIGGER admin_projects_creator_immutable
BEFORE UPDATE OF created_by ON admin_projects
FOR EACH ROW EXECUTE FUNCTION reject_admin_project_creator_change();

-- Stop new runs before reconciling existing work. CREATE TRIGGER takes a table
-- lock that closes the race with a concurrent INSERT while this migration is
-- fencing the existing rows.
CREATE OR REPLACE FUNCTION reject_unavailable_agent_run_project()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $reject_unavailable_agent_run_project$
DECLARE
    project_creator UUID;
BEGIN
    SELECT project.created_by
    INTO project_creator
    FROM admin_projects AS project
    WHERE project.project_id = NEW.project_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'agent_runs project_id must reference an existing project';
    END IF;

    IF project_creator IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'Agents execution is unavailable for self-registered projects';
    END IF;

    RETURN NEW;
END
$reject_unavailable_agent_run_project$;

DROP TRIGGER IF EXISTS agent_runs_operator_project_only ON agent_runs;
CREATE TRIGGER agent_runs_operator_project_only
BEFORE INSERT OR UPDATE OF project_id ON agent_runs
FOR EACH ROW EXECUTE FUNCTION reject_unavailable_agent_run_project();

-- Capture every disallowed run that could still execute or be resumed. Missing
-- project rows are fenced as well: the new trigger rejects that invalid state,
-- so preserving a live legacy row in it would leave an execution loophole.
CREATE TEMP TABLE apdl_disallowed_active_agent_runs
AS
SELECT run.run_id
FROM agent_runs AS run
LEFT JOIN admin_projects AS project ON project.project_id = run.project_id
WHERE (project.project_id IS NULL OR project.created_by IS NOT NULL)
  AND (
      run.status IN ('started', 'running', 'waiting_approval')
      OR (
          run.phase = 'resuming'
          AND run.status IN ('approved', 'rejected')
      )
  );

-- Release exact proposal claims before terminalizing their owners. This uses
-- the same approved queue state as normal abandoned-run recovery.
UPDATE feature_proposals AS proposal
SET status = 'approved',
    claim_run_id = NULL,
    error = NULL,
    updated_at = now()
WHERE proposal.status = 'implementing'
  AND proposal.claim_run_id IN (
      SELECT run_id FROM apdl_disallowed_active_agent_runs
  );

UPDATE agent_runs AS run
SET status = 'failed',
    phase = 'execution_disabled',
    lease_owner_id = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE run.run_id IN (
    SELECT run_id FROM apdl_disallowed_active_agent_runs
);

DROP TABLE apdl_disallowed_active_agent_runs;

-- Remove execution authority already copied into self-registered user
-- memberships. A membership containing only execution roles cannot satisfy
-- the canonical non-empty roles constraint, so remove that unusable row.
DELETE FROM admin_user_projects AS membership
USING admin_projects AS project
WHERE project.project_id = membership.project_id
  AND project.created_by IS NOT NULL
  AND cardinality(
      ARRAY(
          SELECT role.value
          FROM unnest(membership.roles) WITH ORDINALITY AS role(value, position)
          WHERE role.value <> ALL(
              ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[]
          )
          ORDER BY role.position
      )
  ) = 0;

UPDATE admin_user_projects AS membership
SET roles = ARRAY(
    SELECT role.value
    FROM unnest(membership.roles) WITH ORDINALITY AS role(value, position)
    WHERE role.value <> ALL(
        ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[]
    )
    ORDER BY role.position
)
FROM admin_projects AS project
WHERE project.project_id = membership.project_id
  AND project.created_by IS NOT NULL
  AND membership.roles &&
      ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[];

-- Apply the same reduction to credentials. Delete an execution-only credential
-- rather than fabricating a replacement role or violating the non-empty roles
-- constraint; credentials retaining any core/read role remain valid.
DELETE FROM auth_credentials AS credential
USING admin_projects AS project
WHERE project.project_id = credential.project_id
  AND project.created_by IS NOT NULL
  AND cardinality(
      ARRAY(
          SELECT role.value
          FROM unnest(credential.roles) WITH ORDINALITY AS role(value, position)
          WHERE role.value <> ALL(
              ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[]
          )
          ORDER BY role.position
      )
  ) = 0;

UPDATE auth_credentials AS credential
SET roles = ARRAY(
    SELECT role.value
    FROM unnest(credential.roles) WITH ORDINALITY AS role(value, position)
    WHERE role.value <> ALL(
        ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[]
    )
    ORDER BY role.position
)
FROM admin_projects AS project
WHERE project.project_id = credential.project_id
  AND project.created_by IS NOT NULL
  AND credential.roles &&
      ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[];
