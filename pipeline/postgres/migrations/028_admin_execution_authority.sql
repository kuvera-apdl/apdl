-- Canonical project authority for every workflow that can spend LLM/query
-- capacity or enqueue autonomous external effects.
--
-- `admin_projects.created_by` remains immutable provenance.  Operator-created
-- projects are authorized automatically; a self-registered project requires one
-- explicit, immutable operator override carrying durable actor and reason
-- evidence.
CREATE TABLE admin_project_execution_authorizations (
    project_id TEXT PRIMARY KEY
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    authorization_source TEXT NOT NULL
        CHECK (
            authorization_source IN (
                'operator_provisioned',
                'self_registered_override'
            )
        ),
    actor TEXT NOT NULL
        CHECK (
            char_length(actor) BETWEEN 1 AND 512
            AND actor = btrim(actor)
            AND position(chr(10) IN actor) = 0
            AND position(chr(13) IN actor) = 0
        ),
    reason TEXT NOT NULL
        CHECK (
            char_length(reason) BETWEEN 1 AND 2000
            AND reason = btrim(reason)
            AND position(chr(10) IN reason) = 0
            AND position(chr(13) IN reason) = 0
        ),
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION apdl_validate_execution_authorization_provenance()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_validate_execution_authorization_provenance$
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
            MESSAGE = 'execution authorization requires an existing project';
    END IF;

    IF NEW.authorization_source = 'operator_provisioned'
       AND project_creator IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'self-registered projects require an explicit execution override';
    END IF;

    IF NEW.authorization_source = 'self_registered_override'
       AND project_creator IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'operator-provisioned projects cannot use a self-registered override';
    END IF;

    RETURN NEW;
END
$apdl_validate_execution_authorization_provenance$;

CREATE TRIGGER admin_project_execution_authorizations_validate
BEFORE INSERT ON admin_project_execution_authorizations
FOR EACH ROW
EXECUTE FUNCTION apdl_validate_execution_authorization_provenance();

INSERT INTO admin_project_execution_authorizations (
    project_id,
    authorization_source,
    actor,
    reason
)
SELECT
    project.project_id,
    'operator_provisioned',
    'system:migration:028',
    'Existing operator-provisioned project'
FROM admin_projects AS project
WHERE project.created_by IS NULL
ON CONFLICT (project_id) DO NOTHING;

CREATE OR REPLACE FUNCTION apdl_reject_execution_authorization_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_execution_authorization_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'project execution authorizations are immutable';
END
$apdl_reject_execution_authorization_mutation$;

CREATE TRIGGER admin_project_execution_authorizations_immutable
BEFORE UPDATE OR DELETE ON admin_project_execution_authorizations
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_execution_authorization_mutation();

CREATE OR REPLACE FUNCTION apdl_authorize_operator_project()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_authorize_operator_project$
BEGIN
    INSERT INTO admin_project_execution_authorizations (
        project_id,
        authorization_source,
        actor,
        reason
    )
    VALUES (
        NEW.project_id,
        'operator_provisioned',
        'system:admin_project_insert',
        'Operator-provisioned project'
    )
    ON CONFLICT (project_id) DO NOTHING;
    RETURN NEW;
END
$apdl_authorize_operator_project$;

CREATE TRIGGER admin_projects_authorize_operator_execution
AFTER INSERT ON admin_projects
FOR EACH ROW
WHEN (NEW.created_by IS NULL)
EXECUTE FUNCTION apdl_authorize_operator_project();

-- One canonical assertion is shared by membership/credential role guards and
-- every registered execution-bearing table.
CREATE OR REPLACE FUNCTION apdl_assert_execution_project_authorized(
    candidate_project_id TEXT,
    authority_context TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_assert_execution_project_authorized$
BEGIN
    PERFORM 1
    FROM admin_projects AS project
    WHERE project.project_id = candidate_project_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = authority_context || ' requires an existing project';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM admin_project_execution_authorizations AS authorization
        WHERE authorization.project_id = candidate_project_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = authority_context
                || ' requires an operator-provisioned or explicitly authorized project';
    END IF;
END
$apdl_assert_execution_project_authorized$;

-- Reconcile authority that may have been reintroduced after migration 014 but
-- before the durable database guard below existed.
DELETE FROM admin_user_projects AS membership
WHERE NOT EXISTS (
          SELECT 1
          FROM admin_project_execution_authorizations AS authorization
          WHERE authorization.project_id = membership.project_id
      )
  AND membership.roles
      && ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[]
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
WHERE NOT EXISTS (
          SELECT 1
          FROM admin_project_execution_authorizations AS authorization
          WHERE authorization.project_id = membership.project_id
      )
  AND membership.roles
      && ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[];

DELETE FROM auth_credentials AS credential
WHERE NOT EXISTS (
          SELECT 1
          FROM admin_project_execution_authorizations AS authorization
          WHERE authorization.project_id = credential.project_id
      )
  AND credential.roles
      && ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[]
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
WHERE NOT EXISTS (
          SELECT 1
          FROM admin_project_execution_authorizations AS authorization
          WHERE authorization.project_id = credential.project_id
      )
  AND credential.roles
      && ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[];

CREATE OR REPLACE FUNCTION apdl_enforce_execution_roles()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_execution_roles$
BEGIN
    IF NEW.roles
       && ARRAY['agents:run', 'agents:manage', 'agents:approve']::TEXT[] THEN
        PERFORM apdl_assert_execution_project_authorized(
            NEW.project_id,
            TG_TABLE_NAME || ' execution roles'
        );
    END IF;
    RETURN NEW;
END
$apdl_enforce_execution_roles$;

DROP TRIGGER IF EXISTS admin_user_projects_execution_authority
    ON admin_user_projects;
CREATE TRIGGER admin_user_projects_execution_authority
BEFORE INSERT OR UPDATE OF project_id, roles ON admin_user_projects
FOR EACH ROW
EXECUTE FUNCTION apdl_enforce_execution_roles();

DROP TRIGGER IF EXISTS auth_credentials_execution_authority
    ON auth_credentials;
CREATE TRIGGER auth_credentials_execution_authority
BEFORE INSERT OR UPDATE OF project_id, roles ON auth_credentials
FOR EACH ROW
EXECUTE FUNCTION apdl_enforce_execution_roles();

-- Migration 014 encoded provenance directly in the agent_runs trigger. The
-- immutable authorization record supersedes that narrower rule so an audited
-- self-registered override can actually execute.
DROP TRIGGER IF EXISTS agent_runs_operator_project_only ON agent_runs;
DROP FUNCTION IF EXISTS reject_unavailable_agent_run_project();

-- Stop database-owned work that could have been queued through an accidental
-- pre-migration role grant.  Services must be stopped while this migration is
-- applied so an in-process LLM or Codegen task cannot race the terminal write.
UPDATE custom_agent_test_runs AS test_run
SET status = 'failed',
    error = 'Project execution authorization is unavailable',
    finished_at = COALESCE(test_run.finished_at, now()),
    lease_expires_at = LEAST(test_run.lease_expires_at, now())
WHERE test_run.status = 'running'
  AND NOT EXISTS (
      SELECT 1
      FROM admin_project_execution_authorizations AS authorization
      WHERE authorization.project_id = test_run.project_id
  );

UPDATE codegen_changesets AS changeset
SET status = 'error',
    error = 'Project execution authorization is unavailable',
    updated_at = now()
WHERE changeset.status IN ('queued', 'cloning', 'editing', 'pushing', 'pr_open')
  AND NOT EXISTS (
      SELECT 1
      FROM admin_project_execution_authorizations AS authorization
      WHERE authorization.project_id = changeset.project_id
  );

-- Registration is the canonical future-table contract.  Every table that can
-- admit or queue execution must have a non-null TEXT project_id and be
-- registered in the migration that creates it.
CREATE TABLE apdl_execution_table_registry (
    table_name TEXT PRIMARY KEY
        CHECK (table_name ~ '^public\.[a-z][a-z0-9_]*$'),
    project_column TEXT NOT NULL DEFAULT 'project_id'
        CHECK (project_column = 'project_id'),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION apdl_enforce_execution_table_project()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_execution_table_project$
BEGIN
    PERFORM apdl_assert_execution_project_authorized(
        NEW.project_id,
        TG_TABLE_NAME
    );
    RETURN NEW;
END
$apdl_enforce_execution_table_project$;

CREATE OR REPLACE FUNCTION apdl_register_execution_table(target_table REGCLASS)
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_register_execution_table$
DECLARE
    target_schema TEXT;
    target_name TEXT;
    project_column_valid BOOLEAN;
    qualified_name TEXT;
BEGIN
    SELECT namespace.nspname, relation.relname
    INTO target_schema, target_name
    FROM pg_class AS relation
    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE relation.oid = target_table
      AND relation.relkind IN ('r', 'p');

    IF NOT FOUND OR target_schema <> 'public' THEN
        RAISE EXCEPTION
            'execution-bearing table must be a public base or partitioned table';
    END IF;

    SELECT attribute.attnotnull
           AND attribute.atttypid = 'text'::regtype
    INTO project_column_valid
    FROM pg_attribute AS attribute
    WHERE attribute.attrelid = target_table
      AND attribute.attname = 'project_id'
      AND NOT attribute.attisdropped;

    IF project_column_valid IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'execution-bearing table %.% requires a non-null TEXT project_id',
            target_schema,
            target_name;
    END IF;

    qualified_name := format('%I.%I', target_schema, target_name);
    EXECUTE format(
        'DROP TRIGGER IF EXISTS apdl_execution_project_authorized ON %s',
        qualified_name
    );
    EXECUTE format(
        'CREATE TRIGGER apdl_execution_project_authorized '
        'BEFORE INSERT OR UPDATE OF project_id ON %s '
        'FOR EACH ROW EXECUTE FUNCTION '
        'apdl_enforce_execution_table_project()',
        qualified_name
    );

    INSERT INTO apdl_execution_table_registry (table_name)
    VALUES ('public.' || target_name)
    ON CONFLICT (table_name) DO NOTHING;
END
$apdl_register_execution_table$;

SELECT apdl_register_execution_table('public.agent_runs'::regclass);
SELECT apdl_register_execution_table('public.custom_agent_test_runs'::regclass);
SELECT apdl_register_execution_table('public.agent_approval_commands'::regclass);
SELECT apdl_register_execution_table('public.agent_approval_effects'::regclass);
SELECT apdl_register_execution_table(
    'public.agent_mutation_quota_reservations'::regclass
);
SELECT apdl_register_execution_table('public.llm_calls'::regclass);
SELECT apdl_register_execution_table('public.codegen_changesets'::regclass);

CREATE OR REPLACE FUNCTION apdl_assert_execution_table_registry()
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_assert_execution_table_registry$
DECLARE
    registered RECORD;
    relation_oid REGCLASS;
BEGIN
    FOR registered IN
        SELECT registry.table_name
        FROM apdl_execution_table_registry AS registry
        ORDER BY registry.table_name
    LOOP
        relation_oid := to_regclass(registered.table_name);
        IF relation_oid IS NULL THEN
            RAISE EXCEPTION
                'registered execution-bearing table % is missing',
                registered.table_name;
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM pg_trigger AS trigger_record
            WHERE trigger_record.tgrelid = relation_oid
              AND trigger_record.tgname = 'apdl_execution_project_authorized'
              AND NOT trigger_record.tgisinternal
              AND trigger_record.tgenabled <> 'D'
        ) THEN
            RAISE EXCEPTION
                'registered execution-bearing table % is not fenced',
                registered.table_name;
        END IF;
    END LOOP;
END
$apdl_assert_execution_table_registry$;

SELECT apdl_assert_execution_table_registry();

COMMENT ON TABLE admin_project_execution_authorizations IS
    'Immutable operator authority for project-scoped Agents and Codegen execution.';
COMMENT ON TABLE apdl_execution_table_registry IS
    'Canonical registry of project-scoped tables that admit or queue execution.';
