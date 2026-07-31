-- Owner-controlled Agents setup, canonical model assignments, and analysis
-- admission. Ordinary governed analysis is authorized by an active project
-- setup; operator execution authority remains the ceiling for approvals,
-- Codegen, and external effects.

-- Migration 023 fabricated a local policy/model for every project. The
-- project-scoped connection workflow has no local-provider setup contract, so
-- migrate every project to an explicit inactive state and require a deliberate
-- activation against a current normalized remote-provider inventory.
DROP TRIGGER IF EXISTS admin_projects_ensure_llm_policy ON admin_projects;

DELETE FROM llm_project_model_assignments;
DELETE FROM llm_project_provider_policies;

ALTER TABLE llm_project_policies
    ALTER COLUMN project_daily_cost_limit_usd_micros
        SET DEFAULT 20000000,
    ALTER COLUMN run_cost_limit_usd_micros
        SET DEFAULT 2000000,
    ADD COLUMN state TEXT NOT NULL DEFAULT 'inactive'
        CHECK (state IN ('inactive', 'active')),
    ADD COLUMN version BIGINT NOT NULL DEFAULT 0
        CHECK (version >= 0),
    ADD COLUMN activated_by_actor_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    ADD COLUMN activated_at TIMESTAMPTZ,
    ADD COLUMN deactivated_by_actor_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    ADD COLUMN deactivation_reason TEXT,
    ADD COLUMN deactivated_at TIMESTAMPTZ,
    ADD CONSTRAINT llm_project_policies_activation_shape_check CHECK (
        (
            state = 'active'
            AND version > 0
            AND activated_by_actor_user_id IS NOT NULL
            AND activated_at IS NOT NULL
            AND deactivated_by_actor_user_id IS NULL
            AND deactivation_reason IS NULL
            AND deactivated_at IS NULL
        )
        OR (
            state = 'inactive'
            AND (
                (
                    version = 0
                    AND activated_by_actor_user_id IS NULL
                    AND activated_at IS NULL
                    AND deactivated_by_actor_user_id IS NULL
                    AND deactivation_reason IS NULL
                    AND deactivated_at IS NULL
                )
                OR (
                    version > 0
                    AND activated_by_actor_user_id IS NOT NULL
                    AND activated_at IS NOT NULL
                    AND deactivated_by_actor_user_id IS NOT NULL
                    AND deactivation_reason IS NOT NULL
                    AND deactivated_at IS NOT NULL
                )
            )
        )
    ),
    ADD CONSTRAINT llm_project_policies_deactivation_reason_check CHECK (
        deactivation_reason IS NULL
        OR (
            deactivation_reason = BTRIM(deactivation_reason)
            AND LENGTH(deactivation_reason) BETWEEN 1 AND 2000
            AND POSITION(CHR(10) IN deactivation_reason) = 0
            AND POSITION(CHR(13) IN deactivation_reason) = 0
        )
    ),
    ADD CONSTRAINT llm_project_policies_bounded_budget_check CHECK (
        project_daily_cost_limit_usd_micros <= 1000000000000000
        AND run_cost_limit_usd_micros <= 1000000000000000
        AND run_cost_limit_usd_micros <=
            project_daily_cost_limit_usd_micros
    );

-- Inactive setup responses expose the exact safe limits that activation will
-- apply, so the review step never presents the obsolete zero-budget bootstrap
-- values from migration 023.
UPDATE llm_project_policies
SET project_daily_cost_limit_usd_micros = 20000000,
    run_cost_limit_usd_micros = 2000000,
    updated_at = NOW();

CREATE OR REPLACE FUNCTION ensure_llm_project_policy_defaults()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $llm_project_policy_defaults$
BEGIN
    INSERT INTO llm_project_policies (project_id)
    VALUES (NEW.project_id)
    ON CONFLICT (project_id) DO NOTHING;
    RETURN NEW;
END
$llm_project_policy_defaults$;

CREATE TRIGGER admin_projects_ensure_llm_policy
AFTER INSERT ON admin_projects
FOR EACH ROW EXECUTE FUNCTION ensure_llm_project_policy_defaults();

ALTER TABLE llm_project_provider_connections
    ADD COLUMN inventory_version BIGINT;

UPDATE llm_project_provider_connections
SET inventory_version = version;

ALTER TABLE llm_project_provider_connections
    ALTER COLUMN inventory_version SET NOT NULL,
    ADD CONSTRAINT llm_project_provider_connections_inventory_version_check
        CHECK (inventory_version > 0),
    ADD CONSTRAINT llm_project_provider_connections_inventory_identity_key
        UNIQUE (project_id, provider, inventory_version);

ALTER TABLE llm_project_provider_models
    ADD COLUMN inventory_version BIGINT;

UPDATE llm_project_provider_models
SET inventory_version = connection_version,
    pricing_status = 'catalog_reviewed';

ALTER TABLE llm_project_provider_models
    DROP CONSTRAINT IF EXISTS
        llm_project_provider_models_pricing_status_check,
    ALTER COLUMN inventory_version SET NOT NULL,
    ADD CONSTRAINT llm_project_provider_models_pricing_status_check
        CHECK (pricing_status = 'catalog_reviewed'),
    ADD CONSTRAINT llm_project_provider_models_inventory_version_check
        CHECK (inventory_version > 0),
    ADD CONSTRAINT llm_project_provider_models_inventory_identity_key
        UNIQUE (project_id, provider, connection_version, inventory_version, model_id),
    ADD CONSTRAINT llm_project_provider_models_inventory_fk
        FOREIGN KEY (project_id, provider, inventory_version)
        REFERENCES llm_project_provider_connections (
            project_id, provider, inventory_version
        )
        ON DELETE CASCADE;

ALTER TABLE llm_project_model_assignments
    DROP CONSTRAINT llm_project_model_assignments_policy_fk,
    DROP CONSTRAINT llm_project_model_assignments_endpoint_check,
    DROP COLUMN endpoint_url,
    DROP COLUMN assigned_by_actor,
    ADD COLUMN model_catalog_version TEXT NOT NULL
        CHECK (
            model_catalog_version ~ '^llm-provider-catalog@[1-9][0-9]*$'
        ),
    ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD CONSTRAINT llm_project_model_assignments_model_check CHECK (
        LENGTH(model) BETWEEN 1 AND 128
        AND model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'
    ),
    ADD CONSTRAINT llm_project_model_assignments_connection_fk
        FOREIGN KEY (project_id, provider)
        REFERENCES llm_project_provider_connections (project_id, provider)
        ON DELETE RESTRICT;

ALTER TABLE llm_provider_attempts
    ADD COLUMN setup_version BIGINT,
    ADD COLUMN model_tier TEXT,
    ADD COLUMN connection_version BIGINT,
    ADD COLUMN inventory_version BIGINT,
    ADD COLUMN model_catalog_version TEXT,
    ADD COLUMN legacy_unbound_setup BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE llm_provider_attempts
SET legacy_unbound_setup = TRUE;

ALTER TABLE llm_provider_attempts
    ADD CONSTRAINT llm_provider_attempts_setup_binding_check CHECK (
        (
            legacy_unbound_setup
            AND setup_version IS NULL
            AND model_tier IS NULL
            AND connection_version IS NULL
            AND inventory_version IS NULL
            AND model_catalog_version IS NULL
        )
        OR (
            NOT legacy_unbound_setup
            AND setup_version > 0
            AND model_tier IN ('fast', 'reasoning')
            AND connection_version > 0
            AND inventory_version > 0
            AND model_catalog_version
                ~ '^llm-provider-catalog@[1-9][0-9]*$'
        )
    );

CREATE OR REPLACE FUNCTION apdl_protect_llm_attempt_setup_binding()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_protect_llm_attempt_setup_binding$
BEGIN
    IF (
        (TG_OP = 'INSERT' AND NEW.legacy_unbound_setup)
        OR (
            TG_OP = 'UPDATE'
            AND NEW.legacy_unbound_setup
            AND NOT OLD.legacy_unbound_setup
        )
        OR (
            TG_OP = 'UPDATE'
            AND (
                NEW.setup_version IS DISTINCT FROM OLD.setup_version
                OR NEW.model_tier IS DISTINCT FROM OLD.model_tier
                OR NEW.connection_version IS DISTINCT FROM
                    OLD.connection_version
                OR NEW.inventory_version IS DISTINCT FROM
                    OLD.inventory_version
                OR NEW.model_catalog_version IS DISTINCT FROM
                    OLD.model_catalog_version
            )
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'LLM attempt setup binding is immutable';
    END IF;
    RETURN NEW;
END
$apdl_protect_llm_attempt_setup_binding$;

CREATE TRIGGER llm_provider_attempts_protect_setup_binding
BEFORE INSERT OR UPDATE OF
    setup_version, model_tier, connection_version, inventory_version,
    model_catalog_version, legacy_unbound_setup
ON llm_provider_attempts
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_llm_attempt_setup_binding();

CREATE TABLE llm_project_setup_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    action TEXT NOT NULL
        CHECK (action IN ('activate', 'reconfigure', 'deactivate')),
    outcome TEXT NOT NULL CHECK (outcome = 'succeeded'),
    actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    setup_version BIGINT NOT NULL CHECK (setup_version > 0),
    previous_setup JSONB NOT NULL,
    next_setup JSONB NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_project_setup_audit_reason_check CHECK (
        (
            action = 'deactivate'
            AND reason IS NOT NULL
            AND reason = BTRIM(reason)
            AND LENGTH(reason) BETWEEN 1 AND 2000
            AND POSITION(CHR(10) IN reason) = 0
            AND POSITION(CHR(13) IN reason) = 0
        )
        OR (action <> 'deactivate' AND reason IS NULL)
    ),
    CONSTRAINT llm_project_setup_audit_snapshot_check CHECK (
        JSONB_TYPEOF(previous_setup) = 'object'
        AND JSONB_TYPEOF(next_setup) = 'object'
    )
);

CREATE INDEX llm_project_setup_audit_project_created_idx
    ON llm_project_setup_audit (
        project_id, created_at DESC, audit_id DESC
    );

CREATE OR REPLACE FUNCTION apdl_reject_llm_setup_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_llm_setup_audit_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Agents project setup audit rows are immutable';
END
$apdl_reject_llm_setup_audit_mutation$;

CREATE TRIGGER llm_project_setup_audit_no_update_delete
BEFORE UPDATE OR DELETE ON llm_project_setup_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_llm_setup_audit_mutation();

CREATE TRIGGER llm_project_setup_audit_no_truncate
BEFORE TRUNCATE ON llm_project_setup_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_llm_setup_audit_mutation();

-- agents:run and agents:manage are analysis roles. Only agents:approve remains
-- an operator-authorized effect role at the membership/credential boundary.
CREATE OR REPLACE FUNCTION apdl_enforce_execution_roles()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_execution_roles$
BEGIN
    IF 'agents:approve' = ANY(NEW.roles) THEN
        PERFORM apdl_assert_execution_project_authorized(
            NEW.project_id,
            TG_TABLE_NAME || ' effect role'
        );
    END IF;
    RETURN NEW;
END
$apdl_enforce_execution_roles$;

-- Split the execution-table registry by risk. Analysis tables are fenced by
-- active setup, while effect tables remain fenced by immutable operator
-- execution authorization.
DROP TRIGGER IF EXISTS apdl_execution_project_authorized ON agent_runs;
DROP TRIGGER IF EXISTS apdl_execution_project_authorized
    ON custom_agent_test_runs;
DROP TRIGGER IF EXISTS apdl_execution_project_authorized ON llm_calls;

DELETE FROM apdl_execution_table_registry
WHERE table_name IN (
    'public.agent_runs',
    'public.custom_agent_test_runs',
    'public.llm_calls'
);

CREATE TABLE apdl_analysis_table_registry (
    table_name TEXT PRIMARY KEY
        CHECK (table_name ~ '^public\.[a-z][a-z0-9_]*$'),
    project_column TEXT NOT NULL DEFAULT 'project_id'
        CHECK (project_column = 'project_id'),
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION apdl_assert_agents_project_active(
    candidate_project_id TEXT,
    authority_context TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_assert_agents_project_active$
BEGIN
    PERFORM 1
    FROM llm_project_policies AS policy
    WHERE policy.project_id = candidate_project_id
      AND policy.state = 'active'
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = authority_context
                || ' requires active Agents project setup';
    END IF;
END
$apdl_assert_agents_project_active$;

CREATE OR REPLACE FUNCTION apdl_enforce_analysis_table_project()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_analysis_table_project$
BEGIN
    PERFORM apdl_assert_agents_project_active(NEW.project_id, TG_TABLE_NAME);
    RETURN NEW;
END
$apdl_enforce_analysis_table_project$;

CREATE OR REPLACE FUNCTION apdl_register_analysis_table(target_table REGCLASS)
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_register_analysis_table$
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
            'analysis-bearing table must be a public base or partitioned table';
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
            'analysis-bearing table %.% requires a non-null TEXT project_id',
            target_schema,
            target_name;
    END IF;

    qualified_name := format('%I.%I', target_schema, target_name);
    EXECUTE format(
        'DROP TRIGGER IF EXISTS apdl_analysis_project_active ON %s',
        qualified_name
    );
    EXECUTE format(
        'CREATE TRIGGER apdl_analysis_project_active '
        'BEFORE INSERT OR UPDATE OF project_id ON %s '
        'FOR EACH ROW EXECUTE FUNCTION '
        'apdl_enforce_analysis_table_project()',
        qualified_name
    );

    INSERT INTO apdl_analysis_table_registry (table_name)
    VALUES ('public.' || target_name)
    ON CONFLICT (table_name) DO NOTHING;
END
$apdl_register_analysis_table$;

SELECT apdl_register_analysis_table('public.agent_runs'::regclass);
SELECT apdl_register_analysis_table(
    'public.custom_agent_test_runs'::regclass
);
SELECT apdl_register_analysis_table('public.llm_calls'::regclass);
SELECT apdl_register_analysis_table('public.llm_provider_attempts'::regclass);

CREATE OR REPLACE FUNCTION apdl_assert_analysis_table_registry()
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_assert_analysis_table_registry$
DECLARE
    registered RECORD;
    relation_oid REGCLASS;
BEGIN
    FOR registered IN
        SELECT registry.table_name
        FROM apdl_analysis_table_registry AS registry
        ORDER BY registry.table_name
    LOOP
        relation_oid := to_regclass(registered.table_name);
        IF relation_oid IS NULL THEN
            RAISE EXCEPTION
                'registered analysis-bearing table % is missing',
                registered.table_name;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_trigger AS trigger_record
            WHERE trigger_record.tgrelid = relation_oid
              AND trigger_record.tgname = 'apdl_analysis_project_active'
              AND NOT trigger_record.tgisinternal
              AND trigger_record.tgenabled <> 'D'
        ) THEN
            RAISE EXCEPTION
                'registered analysis-bearing table % is not fenced',
                registered.table_name;
        END IF;
    END LOOP;
END
$apdl_assert_analysis_table_registry$;

SELECT apdl_assert_analysis_table_registry();

CREATE OR REPLACE FUNCTION apdl_validate_active_agents_setup(
    candidate_project_id TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_validate_active_agents_setup$
DECLARE
    current_policy RECORD;
    valid_assignment_count INTEGER;
BEGIN
    SELECT state, required_data_residency, allow_cross_vendor_retry,
           project_daily_cost_limit_usd_micros,
           run_cost_limit_usd_micros
    INTO current_policy
    FROM llm_project_policies
    WHERE project_id = candidate_project_id;

    IF NOT FOUND OR current_policy.state <> 'active' THEN
        RETURN;
    END IF;

    IF current_policy.project_daily_cost_limit_usd_micros <= 0
       OR current_policy.run_cost_limit_usd_micros <= 0
       OR current_policy.run_cost_limit_usd_micros >
          current_policy.project_daily_cost_limit_usd_micros THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'active Agents setup requires positive bounded budgets';
    END IF;
    IF current_policy.allow_cross_vendor_retry THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'active Agents setup forbids implicit cross-vendor retry';
    END IF;

    SELECT COUNT(*)
    INTO valid_assignment_count
    FROM llm_project_model_assignments AS assignment
    JOIN llm_project_provider_connections AS connection
     ON connection.project_id = assignment.project_id
     AND connection.provider = assignment.provider
     AND connection.state = 'active'
     AND connection.catalog_version = assignment.model_catalog_version
    JOIN llm_project_provider_models AS model
      ON model.project_id = assignment.project_id
     AND model.provider = assignment.provider
     AND model.connection_version = connection.version
     AND model.inventory_version = connection.inventory_version
     AND model.model_id = assignment.model
     AND model.catalog_version = assignment.model_catalog_version
     AND assignment.tier = ANY(model.supported_tiers)
    JOIN llm_project_provider_credentials AS credential
      ON credential.credential_id = connection.credential_id
     AND credential.project_id = connection.project_id
     AND credential.provider = connection.provider
     AND credential.state = 'active'
    JOIN llm_project_provider_policies AS provider_policy
      ON provider_policy.project_id = assignment.project_id
     AND provider_policy.provider = assignment.provider
     AND provider_policy.model = assignment.model
     AND provider_policy.data_residency =
         current_policy.required_data_residency
     AND provider_policy.data_residency = model.data_residency
     AND provider_policy.allowed_data_classifications =
         model.allowed_data_classifications
     AND provider_policy.enabled
    WHERE assignment.project_id = candidate_project_id;

    IF valid_assignment_count <> 2
       OR NOT EXISTS (
           SELECT 1
           FROM llm_project_model_assignments
           WHERE project_id = candidate_project_id AND tier = 'fast'
       )
       OR NOT EXISTS (
           SELECT 1
           FROM llm_project_model_assignments
           WHERE project_id = candidate_project_id AND tier = 'reasoning'
       )
       OR EXISTS (
           SELECT 1
           FROM llm_project_provider_policies AS provider_policy
           WHERE provider_policy.project_id = candidate_project_id
             AND provider_policy.enabled
             AND NOT EXISTS (
                 SELECT 1
                 FROM llm_project_model_assignments AS assignment
                 WHERE assignment.project_id =
                       provider_policy.project_id
                   AND assignment.provider = provider_policy.provider
                   AND assignment.model = provider_policy.model
             )
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'active Agents setup requires current fast and reasoning assignments';
    END IF;
END
$apdl_validate_active_agents_setup$;

CREATE OR REPLACE FUNCTION apdl_check_active_agents_setup_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_check_active_agents_setup_trigger$
BEGIN
    PERFORM apdl_validate_active_agents_setup(
        COALESCE(NEW.project_id, OLD.project_id)
    );
    RETURN NULL;
END
$apdl_check_active_agents_setup_trigger$;

CREATE CONSTRAINT TRIGGER llm_project_policies_validate_active_setup
AFTER INSERT OR UPDATE ON llm_project_policies
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

CREATE CONSTRAINT TRIGGER llm_project_model_assignments_validate_active_setup
AFTER INSERT OR UPDATE OR DELETE ON llm_project_model_assignments
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

CREATE CONSTRAINT TRIGGER llm_project_provider_policies_validate_active_setup
AFTER INSERT OR UPDATE OR DELETE ON llm_project_provider_policies
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

CREATE CONSTRAINT TRIGGER llm_project_provider_connections_validate_active_setup
AFTER INSERT OR UPDATE OR DELETE ON llm_project_provider_connections
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

CREATE CONSTRAINT TRIGGER llm_project_provider_models_validate_active_setup
AFTER INSERT OR UPDATE OR DELETE ON llm_project_provider_models
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

CREATE CONSTRAINT TRIGGER llm_project_provider_credentials_validate_active_setup
AFTER UPDATE OR DELETE ON llm_project_provider_credentials
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

COMMENT ON TABLE llm_project_model_assignments IS
    'Canonical fast/reasoning assignments bound to exact current project inventories.';
COMMENT ON TABLE llm_project_setup_audit IS
    'Immutable, non-secret owner/delegate Agents activation history.';
COMMENT ON TABLE apdl_analysis_table_registry IS
    'Canonical registry of project-scoped tables admitted by active Agents setup.';
COMMENT ON TABLE admin_project_execution_authorizations IS
    'Immutable operator authority for approvals, Codegen, and external effects.';
