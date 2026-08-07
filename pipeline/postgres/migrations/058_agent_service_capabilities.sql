-- Migration 058: execution-scoped service capabilities and Agents isolation.
--
-- Agents mints short-lived, hash-only capabilities that are bound to one live
-- execution, one exact audience/role shape, and (for mutations) one request
-- digest.  Ordinary runtime services may validate capabilities but cannot mint
-- or revoke them.  Agents and the two SECURITY DEFINER boundaries use fixed,
-- non-inheriting roles provisioned before the migration starts.

DO $apdl_validate_service_capability_roles$
DECLARE
    fixed_role_oids OID[];
BEGIN
    IF (
        SELECT array_agg(role.rolname::TEXT ORDER BY role.rolname)
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY (ARRAY[
            'apdl_runtime',
            'apdl_agents',
            'apdl_llm_vault',
            'apdl_audit_operator',
            'apdl_audit_purge_definer',
            'apdl_project_authority_definer',
            'apdl_capability_consumer_definer'
        ]::TEXT[])
    ) IS DISTINCT FROM ARRAY[
        'apdl_agents',
        'apdl_audit_operator',
        'apdl_audit_purge_definer',
        'apdl_capability_consumer_definer',
        'apdl_llm_vault',
        'apdl_project_authority_definer',
        'apdl_runtime'
    ]::TEXT[] THEN
        RAISE EXCEPTION
            'required fixed roles are missing; provision database roles before migration 058';
    END IF;

    -- Migration 056 deliberately permits a NOLOGIN vault role when the vault
    -- service password is omitted in schema-only environments. Runtime and
    -- Agents always require login roles; every service role remains otherwise
    -- unprivileged and non-inheriting.
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY (ARRAY[
            'apdl_runtime', 'apdl_agents', 'apdl_llm_vault'
        ]::TEXT[])
          AND (
              role.rolsuper
              OR role.rolinherit
              OR role.rolcreaterole
              OR role.rolcreatedb
              OR role.rolreplication
              OR role.rolbypassrls
              OR (
                  role.rolname IN ('apdl_runtime', 'apdl_agents')
                  AND NOT role.rolcanlogin
              )
          )
    ) THEN
        RAISE EXCEPTION
            'APDL service roles have non-canonical attributes';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY (ARRAY[
            'apdl_audit_operator',
            'apdl_audit_purge_definer',
            'apdl_project_authority_definer',
            'apdl_capability_consumer_definer'
        ]::TEXT[])
          AND (
              role.rolcanlogin
              OR role.rolsuper
              OR role.rolinherit
              OR role.rolcreaterole
              OR role.rolcreatedb
              OR role.rolreplication
              OR role.rolbypassrls
          )
    ) THEN
        RAISE EXCEPTION
            'APDL authority roles must be NOLOGIN NOINHERIT and unprivileged';
    END IF;

    SELECT array_agg(role.oid)
    INTO fixed_role_oids
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = ANY (ARRAY[
        'apdl_runtime',
        'apdl_agents',
        'apdl_llm_vault',
        'apdl_audit_operator',
        'apdl_audit_purge_definer',
        'apdl_project_authority_definer',
        'apdl_capability_consumer_definer'
    ]::TEXT[]);

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.roleid = ANY (fixed_role_oids)
           OR membership.member = ANY (fixed_role_oids)
    ) THEN
        RAISE EXCEPTION
            'fixed APDL roles must not participate in role membership';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database AS database
        JOIN pg_catalog.pg_roles AS role ON role.oid = database.datdba
        WHERE role.rolname = 'apdl_agents'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_roles AS role ON role.oid = namespace.nspowner
        WHERE role.rolname = 'apdl_agents'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend AS dependency
        JOIN pg_catalog.pg_roles AS role ON role.oid = dependency.refobjid
        WHERE dependency.refclassid =
                  'pg_catalog.pg_authid'::pg_catalog.regclass
          AND dependency.deptype = 'o'
          AND role.rolname = 'apdl_agents'
    ) THEN
        RAISE EXCEPTION
            'apdl_agents must not own a database, schema, or database object';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend AS dependency
        JOIN pg_catalog.pg_roles AS role ON role.oid = dependency.refobjid
        WHERE dependency.refclassid =
                  'pg_catalog.pg_authid'::pg_catalog.regclass
          AND dependency.deptype = 'o'
          AND role.rolname IN (
              'apdl_project_authority_definer',
              'apdl_capability_consumer_definer'
          )
    ) THEN
        RAISE EXCEPTION
            'new APDL authority roles must not own preexisting database objects';
    END IF;
END
$apdl_validate_service_capability_roles$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public
    FROM apdl_runtime, apdl_agents, apdl_llm_vault,
         apdl_audit_operator, apdl_audit_purge_definer,
         apdl_project_authority_definer,
         apdl_capability_consumer_definer;

CREATE FUNCTION public.apdl_canonical_capability_audiences(
    selected_audiences TEXT[]
)
RETURNS TEXT[]
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT COALESCE(
        ARRAY_AGG(allowed.audience ORDER BY allowed.position),
        ARRAY[]::TEXT[]
    )
    FROM UNNEST(
        ARRAY['config', 'query', 'codegen']::TEXT[]
    ) WITH ORDINALITY AS allowed(audience, position)
    WHERE allowed.audience = ANY(selected_audiences)
$$;

CREATE FUNCTION public.apdl_canonical_capability_roles(
    selected_roles TEXT[]
)
RETURNS TEXT[]
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT COALESCE(
        ARRAY_AGG(allowed.role ORDER BY allowed.position),
        ARRAY[]::TEXT[]
    )
    FROM UNNEST(ARRAY[
        'config:write',
        'config:evaluate',
        'query:read',
        'agents:read',
        'agents:manage'
    ]::TEXT[]) WITH ORDINALITY AS allowed(role, position)
    WHERE allowed.role = ANY(selected_roles)
$$;

CREATE TABLE public.agent_service_capabilities (
    capability_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash CHAR(64) NOT NULL UNIQUE,
    project_id TEXT NOT NULL CHECK (
        project_id ~ '^[A-Za-z0-9]{1,64}$'
    ) REFERENCES public.admin_projects(project_id) ON DELETE CASCADE,
    execution_kind TEXT NOT NULL CHECK (
        execution_kind IN (
            'agent_run', 'custom_agent_test', 'approval_effect'
        )
    ),
    execution_id TEXT NOT NULL CHECK (
        char_length(execution_id) BETWEEN 1 AND 128
        AND execution_id = btrim(execution_id)
        AND execution_id !~ '[[:space:]]'
    ),
    run_id TEXT NOT NULL CHECK (
        char_length(run_id) BETWEEN 1 AND 128
        AND run_id = btrim(run_id)
        AND run_id !~ '[[:space:]]'
    ),
    execution_owner_id TEXT NOT NULL CHECK (
        char_length(execution_owner_id) BETWEEN 1 AND 512
        AND execution_owner_id = btrim(execution_owner_id)
        AND execution_owner_id !~ '[[:space:]]'
    ),
    audiences TEXT[] NOT NULL CHECK (
        cardinality(audiences) > 0
        AND audiences = public.apdl_canonical_capability_audiences(audiences)
    ),
    roles TEXT[] NOT NULL CHECK (
        cardinality(roles) > 0
        AND roles = public.apdl_canonical_capability_roles(roles)
    ),
    request_sha256 CHAR(64) CHECK (
        request_sha256 IS NULL OR request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
        CONSTRAINT agent_service_capabilities_expiry_check CHECK (
            expires_at > issued_at
            AND expires_at <= issued_at + INTERVAL '5 minutes'
        ),
    consumed_at TIMESTAMPTZ
        CONSTRAINT agent_service_capabilities_consumed_at_check CHECK (
            consumed_at IS NULL
            OR (consumed_at >= issued_at AND consumed_at <= expires_at)
        ),
    CONSTRAINT agent_service_capabilities_token_hash_check CHECK (
        token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT agent_service_capabilities_authority_shape_check CHECK (
        (audiences = ARRAY['config']::TEXT[]
            AND roles = ARRAY['config:write']::TEXT[])
        OR (audiences = ARRAY['config']::TEXT[]
            AND roles = ARRAY['config:evaluate']::TEXT[])
        OR (audiences = ARRAY['config']::TEXT[]
            AND roles = ARRAY['agents:read']::TEXT[])
        OR (audiences = ARRAY['config']::TEXT[]
            AND roles = ARRAY['query:read']::TEXT[])
        OR (audiences = ARRAY['query']::TEXT[]
            AND roles = ARRAY['query:read']::TEXT[])
        OR (audiences = ARRAY['config', 'query']::TEXT[]
            AND roles = ARRAY['query:read']::TEXT[])
        OR (audiences = ARRAY['codegen']::TEXT[]
            AND roles = ARRAY['agents:read']::TEXT[])
        OR (audiences = ARRAY['codegen']::TEXT[]
            AND roles = ARRAY['agents:manage']::TEXT[])
    ),
    CONSTRAINT agent_service_capabilities_mutation_binding_check CHECK (
        (
            roles IN (
                ARRAY['config:write']::TEXT[],
                ARRAY['agents:manage']::TEXT[]
            )
            AND execution_kind = 'approval_effect'
            AND request_sha256 IS NOT NULL
        ) OR (
            roles <> ARRAY['config:write']::TEXT[]
            AND roles <> ARRAY['agents:manage']::TEXT[]
            AND request_sha256 IS NULL
            AND consumed_at IS NULL
        )
    )
);

CREATE INDEX agent_service_capabilities_expiry_idx
    ON public.agent_service_capabilities (expires_at);

COMMENT ON TABLE public.agent_service_capabilities IS
    'Hash-only, short-lived authority for one leased Agents execution to call exact internal service audiences.';

CREATE OR REPLACE FUNCTION public.apdl_assert_execution_project_authorized(
    candidate_project_id TEXT,
    authority_context TEXT
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apdl_assert_execution_project_authorized$
BEGIN
    PERFORM 1
    FROM public.admin_projects AS project
    WHERE project.project_id = candidate_project_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = authority_context || ' requires an existing project';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.admin_project_execution_authorizations AS execution_authority
        WHERE execution_authority.project_id = candidate_project_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = authority_context
                || ' requires an operator-provisioned or explicitly authorized project';
    END IF;
END
$apdl_assert_execution_project_authorized$;

CREATE FUNCTION public.apdl_project_management_authority(
    candidate_project_id TEXT,
    candidate_actor_user_id UUID
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apdl_project_management_authority$
DECLARE
    project_owner_user_id UUID;
    actor_active BOOLEAN;
    actor_roles TEXT[];
BEGIN
    SELECT project.owner_user_id, account.active
    INTO project_owner_user_id, actor_active
    FROM public.admin_projects AS project
    JOIN public.admin_users AS account
      ON account.user_id = candidate_actor_user_id
    WHERE project.project_id = candidate_project_id
    FOR SHARE OF project, account;

    IF NOT FOUND OR actor_active IS NOT TRUE THEN
        RETURN 'none';
    END IF;
    IF project_owner_user_id = candidate_actor_user_id THEN
        RETURN 'owner';
    END IF;

    SELECT membership.roles
    INTO actor_roles
    FROM public.admin_user_projects AS membership
    WHERE membership.project_id = candidate_project_id
      AND membership.user_id = candidate_actor_user_id
    FOR SHARE;

    IF COALESCE(
        actor_roles @> ARRAY['agents:manage', 'credentials:manage']::TEXT[],
        FALSE
    ) THEN
        RETURN 'delegated';
    END IF;
    RETURN 'none';
END
$apdl_project_management_authority$;

CREATE FUNCTION public.apdl_agents_grant_owner_execution_roles(
    candidate_project_id TEXT,
    candidate_actor_user_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apdl_agents_grant_owner_execution_roles$
DECLARE
    previous_roles TEXT[];
    next_roles TEXT[];
    actor_email TEXT;
BEGIN
    SELECT membership.roles, account.email
    INTO previous_roles, actor_email
    FROM public.admin_projects AS project
    JOIN public.admin_user_projects AS membership
      ON membership.project_id = project.project_id
     AND membership.user_id = candidate_actor_user_id
    JOIN public.admin_users AS account
      ON account.user_id = membership.user_id
     AND account.active
    WHERE project.project_id = candidate_project_id
      AND project.owner_user_id = candidate_actor_user_id
    FOR SHARE OF project, account
    FOR UPDATE OF membership;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'Agents execution roles require the active project owner';
    END IF;

    next_roles := public.apdl_canonical_admin_roles(
        previous_roles || ARRAY['agents:run', 'agents:manage']::TEXT[]
    );
    IF next_roles = previous_roles THEN
        RETURN FALSE;
    END IF;

    UPDATE public.admin_user_projects
    SET roles = next_roles
    WHERE project_id = candidate_project_id
      AND user_id = candidate_actor_user_id;

    INSERT INTO public.admin_project_membership_audit (
        project_id,
        action,
        actor_user_id,
        subject_user_id,
        subject_email,
        previous_roles,
        new_roles
    ) VALUES (
        candidate_project_id,
        'activation_grant',
        candidate_actor_user_id,
        candidate_actor_user_id,
        actor_email,
        previous_roles,
        next_roles
    );
    RETURN TRUE;
END
$apdl_agents_grant_owner_execution_roles$;

CREATE FUNCTION public.apdl_consume_agent_service_capability(
    candidate_capability_id UUID,
    candidate_token_hash TEXT,
    candidate_audience TEXT,
    candidate_role TEXT,
    candidate_request_sha256 TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
STRICT
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apdl_consume_agent_service_capability$
DECLARE
    consumed_capability_id UUID;
BEGIN
    IF (candidate_audience, candidate_role) NOT IN (
        ('config', 'config:write'),
        ('codegen', 'agents:manage')
    ) THEN
        RETURN FALSE;
    END IF;

    UPDATE public.agent_service_capabilities AS capability
    SET consumed_at = statement_timestamp()
    WHERE capability.capability_id = candidate_capability_id
      AND capability.token_hash = candidate_token_hash
      AND capability.execution_kind = 'approval_effect'
      AND capability.audiences = ARRAY[candidate_audience]
      AND capability.roles = ARRAY[candidate_role]
      AND capability.request_sha256 = candidate_request_sha256
      AND capability.consumed_at IS NULL
      AND capability.issued_at <= statement_timestamp()
      AND capability.expires_at > statement_timestamp()
      AND EXISTS (
          SELECT 1
          FROM public.agent_approval_effects AS effect
          JOIN public.agent_runs AS run
            ON run.run_id = effect.run_id
           AND run.project_id = effect.project_id
          WHERE effect.effect_id::TEXT = capability.execution_id
            AND effect.run_id = capability.run_id
            AND effect.project_id = capability.project_id
            AND effect.status = 'processing'
            AND effect.lease_owner_id = capability.execution_owner_id
            AND effect.lease_expires_at > statement_timestamp()
            AND run.execution_lane_project_id = run.project_id
            AND run.status IN ('approval_queued', 'cancelling')
      )
    RETURNING capability.capability_id INTO consumed_capability_id;

    RETURN consumed_capability_id IS NOT NULL;
END
$apdl_consume_agent_service_capability$;

-- The old Boolean vault-only predicate is replaced by one canonical authority
-- classification shared by Agents and the vault.
REVOKE ALL ON FUNCTION public.apdl_llm_vault_has_management_authority(
    TEXT,
    UUID
) FROM PUBLIC, apdl_runtime, apdl_agents, apdl_llm_vault;
DROP FUNCTION public.apdl_llm_vault_has_management_authority(TEXT, UUID);

-- Start the three new roles from an explicit in-schema privilege floor.  These
-- roles are dedicated APDL identities and may not carry ambient authority from
-- an earlier or partially provisioned deployment.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
    FROM apdl_agents, apdl_project_authority_definer,
         apdl_capability_consumer_definer;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
    FROM apdl_agents, apdl_project_authority_definer,
         apdl_capability_consumer_definer;
REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public
    FROM apdl_agents, apdl_project_authority_definer,
         apdl_capability_consumer_definer;

DO $apdl_grant_agents_database_connect$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO apdl_agents',
        current_database()
    );
END
$apdl_grant_agents_database_connect$;

GRANT USAGE ON SCHEMA public TO apdl_agents;
GRANT SELECT ON
    public.admin_project_execution_authorizations,
    public.agent_approval_commands,
    public.agent_approval_decisions,
    public.agent_approval_effects,
    public.agent_audit_log,
    public.agent_memory,
    public.agent_mutation_quota_reservations,
    public.agent_run_results,
    public.agent_runs,
    public.agent_service_capabilities,
    public.apdl_schema_migrations,
    public.auth_credentials,
    public.custom_agent_test_runs,
    public.custom_agents,
    public.designed_experiments,
    public.experiment_verdicts,
    public.feature_proposals,
    public.llm_calls,
    public.llm_project_model_assignments,
    public.llm_project_policies,
    public.llm_project_provider_connections,
    public.llm_project_provider_models,
    public.llm_project_provider_policies,
    public.llm_project_setup_audit,
    public.llm_provider_attempts,
    public.llm_vault_connection_consumers,
    public.llm_vault_provider_credentials
TO apdl_agents;
GRANT SELECT (project_id, created_by, owner_user_id)
    ON public.admin_projects TO apdl_agents;
GRANT SELECT (user_id, email, active)
    ON public.admin_users TO apdl_agents;
GRANT SELECT (project_id, user_id, roles)
    ON public.admin_user_projects TO apdl_agents;

GRANT INSERT, UPDATE ON
    public.agent_approval_commands,
    public.agent_approval_effects,
    public.agent_run_results,
    public.agent_runs,
    public.custom_agent_test_runs,
    public.custom_agents,
    public.designed_experiments,
    public.experiment_verdicts,
    public.feature_proposals,
    public.llm_calls,
    public.llm_provider_attempts
TO apdl_agents;
GRANT UPDATE ON public.llm_project_policies TO apdl_agents;
GRANT INSERT, DELETE ON
    public.agent_memory,
    public.llm_project_model_assignments,
    public.llm_project_provider_policies
TO apdl_agents;
GRANT INSERT (
    token_hash,
    project_id,
    execution_kind,
    execution_id,
    run_id,
    execution_owner_id,
    audiences,
    roles,
    request_sha256,
    expires_at
) ON public.agent_service_capabilities TO apdl_agents;
GRANT DELETE ON public.agent_service_capabilities TO apdl_agents;
GRANT INSERT ON
    public.agent_approval_decisions,
    public.agent_audit_log,
    public.agent_mutation_quota_reservations,
    public.llm_project_setup_audit
TO apdl_agents;
GRANT USAGE, SELECT ON
    public.agent_audit_log_id_seq,
    public.agent_memory_id_seq,
    public.experiment_verdicts_id_seq
TO apdl_agents;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO apdl_agents;

-- Migration 044's default privileges grant the shared runtime DML on every new
-- table.  Runtime services may read capabilities but may consume them only via
-- the constrained SECURITY DEFINER function below.
GRANT SELECT ON public.agent_service_capabilities TO apdl_runtime;
REVOKE INSERT, UPDATE, DELETE
    ON public.agent_service_capabilities
    FROM apdl_runtime;

REVOKE UPDATE, DELETE
    ON public.config_outbox_operator_log
    FROM apdl_agents;
REVOKE INSERT, UPDATE, DELETE
    ON public.experiment_audit_purge_log
    FROM apdl_agents;
REVOKE UPDATE, DELETE
    ON public.experiment_audit_log
    FROM apdl_agents;
REVOKE ALL ON FUNCTION public.apdl_purge_experiment_audit(
    TEXT,
    TIMESTAMPTZ,
    TEXT,
    TEXT
) FROM apdl_agents;

-- Preserve the vault boundary from migration 056 after the Agents grants.
REVOKE ALL ON
    public.llm_vault_connections,
    public.llm_vault_provider_credentials,
    public.llm_vault_provider_secrets,
    public.llm_vault_connection_consumers,
    public.llm_vault_provider_models,
    public.llm_vault_audit,
    public.llm_vault_access_audit,
    public.llm_vault_key_rotation_audit
FROM apdl_agents;
GRANT SELECT ON
    public.llm_vault_provider_credentials,
    public.llm_vault_connection_consumers
TO apdl_agents;

GRANT USAGE ON SCHEMA public TO apdl_capability_consumer_definer;
GRANT SELECT ON public.agent_service_capabilities
TO apdl_capability_consumer_definer;
GRANT SELECT (
    effect_id,
    run_id,
    project_id,
    status,
    lease_owner_id,
    lease_expires_at
) ON public.agent_approval_effects TO apdl_capability_consumer_definer;
GRANT SELECT (
    run_id,
    project_id,
    execution_lane_project_id,
    status
) ON public.agent_runs TO apdl_capability_consumer_definer;
GRANT UPDATE (consumed_at) ON public.agent_service_capabilities
TO apdl_capability_consumer_definer;
GRANT CREATE ON SCHEMA public TO apdl_capability_consumer_definer;
ALTER FUNCTION public.apdl_consume_agent_service_capability(
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) OWNER TO apdl_capability_consumer_definer;
REVOKE CREATE ON SCHEMA public FROM apdl_capability_consumer_definer;
REVOKE ALL ON FUNCTION public.apdl_consume_agent_service_capability(
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) FROM PUBLIC, apdl_agents, apdl_llm_vault;
GRANT EXECUTE ON FUNCTION public.apdl_consume_agent_service_capability(
    UUID,
    TEXT,
    TEXT,
    TEXT,
    TEXT
) TO apdl_runtime;

GRANT USAGE ON SCHEMA public TO apdl_project_authority_definer;
GRANT SELECT, UPDATE ON
    public.admin_projects,
    public.admin_users,
    public.admin_user_projects
TO apdl_project_authority_definer;
GRANT SELECT ON public.admin_project_execution_authorizations
TO apdl_project_authority_definer;
GRANT INSERT ON public.admin_project_membership_audit
TO apdl_project_authority_definer;
GRANT EXECUTE ON FUNCTION public.apdl_canonical_admin_roles(TEXT[])
TO apdl_project_authority_definer;
GRANT CREATE ON SCHEMA public TO apdl_project_authority_definer;
ALTER FUNCTION public.apdl_project_management_authority(
    TEXT,
    UUID
) OWNER TO apdl_project_authority_definer;
ALTER FUNCTION public.apdl_agents_grant_owner_execution_roles(
    TEXT,
    UUID
) OWNER TO apdl_project_authority_definer;
ALTER FUNCTION public.apdl_assert_execution_project_authorized(
    TEXT,
    TEXT
) OWNER TO apdl_project_authority_definer;
REVOKE CREATE ON SCHEMA public FROM apdl_project_authority_definer;

REVOKE ALL ON FUNCTION public.apdl_project_management_authority(
    TEXT,
    UUID
) FROM PUBLIC, apdl_runtime;
GRANT EXECUTE ON FUNCTION public.apdl_project_management_authority(
    TEXT,
    UUID
) TO apdl_agents, apdl_llm_vault;
REVOKE ALL ON FUNCTION public.apdl_agents_grant_owner_execution_roles(
    TEXT,
    UUID
) FROM PUBLIC, apdl_runtime, apdl_llm_vault;
GRANT EXECUTE ON FUNCTION public.apdl_agents_grant_owner_execution_roles(
    TEXT,
    UUID
) TO apdl_agents;
REVOKE ALL ON FUNCTION public.apdl_assert_execution_project_authorized(
    TEXT,
    TEXT
) FROM PUBLIC, apdl_llm_vault;
GRANT EXECUTE ON FUNCTION public.apdl_assert_execution_project_authorized(
    TEXT,
    TEXT
) TO apdl_runtime, apdl_agents;

DO $apdl_validate_service_capability_boundary$
DECLARE
    capability_consumer_oid OID;
    capability_function_oid OID;
    project_authority_oid OID;
    project_authority_function_oids OID[];
BEGIN
    IF pg_catalog.has_schema_privilege(
        'apdl_agents', 'public', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(
        'apdl_project_authority_definer', 'public', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(
        'apdl_capability_consumer_definer', 'public', 'CREATE'
    ) THEN
        RAISE EXCEPTION
            'service capability roles must not create objects in schema public';
    END IF;

    IF NOT pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'SELECT'
    ) OR pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'INSERT'
    ) OR pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'DELETE'
    ) THEN
        RAISE EXCEPTION
            'apdl_runtime capability privileges are not read-only';
    END IF;

    IF NOT pg_catalog.has_table_privilege(
        'apdl_agents', 'public.agent_service_capabilities', 'SELECT'
    ) OR NOT pg_catalog.has_table_privilege(
        'apdl_agents', 'public.agent_service_capabilities', 'DELETE'
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents', 'public.agent_service_capabilities', 'INSERT'
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents', 'public.agent_service_capabilities', 'UPDATE'
    ) THEN
        RAISE EXCEPTION
            'apdl_agents capability table privileges are not exact';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid =
                  'public.agent_service_capabilities'::pg_catalog.regclass
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
          AND pg_catalog.has_column_privilege(
              'apdl_agents', attribute.attrelid, attribute.attnum, 'INSERT'
          ) <> (attribute.attname = ANY (ARRAY[
              'token_hash',
              'project_id',
              'execution_kind',
              'execution_id',
              'run_id',
              'execution_owner_id',
              'audiences',
              'roles',
              'request_sha256',
              'expires_at'
          ]::NAME[]))
    ) THEN
        RAISE EXCEPTION
            'apdl_agents capability INSERT columns are not exact';
    END IF;

    SELECT role.oid
    INTO capability_consumer_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = 'apdl_capability_consumer_definer';
    capability_function_oid := pg_catalog.to_regprocedure(
        'public.apdl_consume_agent_service_capability(uuid,text,text,text,text)'
    );
    IF capability_function_oid IS NULL OR NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS procedure
        WHERE procedure.oid = capability_function_oid
          AND procedure.proowner = capability_consumer_oid
          AND procedure.prosecdef
          AND procedure.proconfig IS NOT DISTINCT FROM
              ARRAY['search_path=pg_catalog, public']::TEXT[]
    ) THEN
        RAISE EXCEPTION
            'capability consume function definition is invalid';
    END IF;

    IF (
        SELECT count(*) = 2
           AND count(*) FILTER (
               WHERE acl.grantee = capability_consumer_oid
                 AND acl.privilege_type = 'EXECUTE'
                 AND NOT acl.is_grantable
           ) = 1
           AND count(*) FILTER (
               WHERE acl.grantee = (
                   SELECT oid
                   FROM pg_catalog.pg_roles
                   WHERE rolname = 'apdl_runtime'
               )
                 AND acl.privilege_type = 'EXECUTE'
                 AND NOT acl.is_grantable
           ) = 1
        FROM pg_catalog.pg_proc AS procedure
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(
                procedure.proacl,
                pg_catalog.acldefault('f', procedure.proowner)
            )
        ) AS acl
        WHERE procedure.oid = capability_function_oid
    ) IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'capability consume function ACL is not exact';
    END IF;

    SELECT role.oid
    INTO project_authority_oid
    FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = 'apdl_project_authority_definer';
    project_authority_function_oids := ARRAY[
        pg_catalog.to_regprocedure(
            'public.apdl_project_management_authority(text,uuid)'
        ),
        pg_catalog.to_regprocedure(
            'public.apdl_agents_grant_owner_execution_roles(text,uuid)'
        ),
        pg_catalog.to_regprocedure(
            'public.apdl_assert_execution_project_authorized(text,text)'
        )
    ];
    IF array_position(project_authority_function_oids, NULL) IS NOT NULL
       OR EXISTS (
           SELECT 1
           FROM unnest(project_authority_function_oids) AS expected(oid)
           JOIN pg_catalog.pg_proc AS procedure ON procedure.oid = expected.oid
           WHERE procedure.proowner <> project_authority_oid
              OR NOT procedure.prosecdef
              OR procedure.proconfig IS DISTINCT FROM
                  ARRAY['search_path=pg_catalog, public']::TEXT[]
       ) THEN
        RAISE EXCEPTION
            'project authority function ownership is invalid';
    END IF;

    IF pg_catalog.to_regprocedure(
        'public.apdl_llm_vault_has_management_authority(text,uuid)'
    ) IS NOT NULL THEN
        RAISE EXCEPTION
            'retired vault-only management authority function still exists';
    END IF;
END
$apdl_validate_service_capability_boundary$;
