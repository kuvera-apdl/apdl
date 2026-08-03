#!/usr/bin/env bash
# Exact PostgreSQL proof for the migration-044 operator and migration-058
# service-capability boundaries.
#
# Run only against a disposable database after the canonical migrations. The
# script creates and drops one temporary login role and leaves uniquely named
# audit evidence in the disposable database.

set -Eeuo pipefail

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${APDL_OWNER_POSTGRES_USER:?APDL_OWNER_POSTGRES_USER is required}"
: "${APDL_OWNER_POSTGRES_PASSWORD:?APDL_OWNER_POSTGRES_PASSWORD is required}"
: "${APDL_RUNTIME_POSTGRES_PASSWORD:?APDL_RUNTIME_POSTGRES_PASSWORD is required}"
: "${APDL_RUNTIME_TEST_POSTGRES_URL:?APDL_RUNTIME_TEST_POSTGRES_URL is required}"

PGPORT="${PGPORT:-5432}"
operator_role="apdl_audit_operator"
definer_role="apdl_audit_purge_definer"
test_actor="apdl_audit_privilege_test"
test_actor_password="audit_test_only_$(date -u +%s)_$$"
test_suffix="$(date -u +%s)$$"
project_a="roleproofa${test_suffix}"
project_b="roleproofb${test_suffix}"
reason="automated privilege boundary proof"
confirmation="PURGE EXPERIMENT AUDIT"

owner_psql() {
    PGPASSWORD="$APDL_OWNER_POSTGRES_PASSWORD" psql \
        --no-psqlrc \
        --set=ON_ERROR_STOP=1 \
        --quiet \
        --tuples-only \
        --no-align \
        --host "$PGHOST" \
        --port "$PGPORT" \
        --username "$APDL_OWNER_POSTGRES_USER" \
        --dbname "$PGDATABASE" \
        "$@"
}

runtime_psql() {
    PGPASSWORD="$APDL_RUNTIME_POSTGRES_PASSWORD" psql \
        --no-psqlrc \
        --set=ON_ERROR_STOP=1 \
        --quiet \
        --tuples-only \
        --no-align \
        --dbname "$APDL_RUNTIME_TEST_POSTGRES_URL" \
        "$@"
}

operator_psql() {
    PGPASSWORD="$test_actor_password" psql \
        --no-psqlrc \
        --set=ON_ERROR_STOP=1 \
        --quiet \
        --tuples-only \
        --no-align \
        --host "$PGHOST" \
        --port "$PGPORT" \
        --username "$test_actor" \
        --dbname "$PGDATABASE" \
        "$@"
}

cleanup() {
    owner_psql -c "DROP ROLE IF EXISTS $test_actor" \
        >/dev/null 2>&1 || true
}
trap cleanup EXIT

expect_failure() {
    local principal="$1"
    local sql="$2"
    local expected_diagnostic="${3:-}"
    local output
    local -a command

    case "$principal" in
        owner) command=(owner_psql) ;;
        runtime) command=(runtime_psql) ;;
        operator) command=(operator_psql) ;;
        *)
            echo "Unknown PostgreSQL test principal: $principal" >&2
            return 1
            ;;
    esac

    if output="$("${command[@]}" -c "$sql" 2>&1)"; then
        echo "Expected $principal statement to fail: $sql" >&2
        return 1
    fi
    [ -n "$output" ] || {
        echo "Failed $principal statement returned no PostgreSQL diagnostic" >&2
        return 1
    }
    if [ -n "$expected_diagnostic" ] \
        && [[ "$output" != *"$expected_diagnostic"* ]]; then
        echo "Unexpected $principal failure diagnostic: $output" >&2
        return 1
    fi
}

owner_psql \
    --set=test_actor_password="$test_actor_password" <<'SQL'
DO $assert_roles$
DECLARE
    canonical_function_oid OID;
    agents_role_oid OID;
    current_database_oid OID;
    definer_role_oid OID;
    operator_role_oid OID;
    runtime_role_oid OID;
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'apdl_agents'
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolbypassrls
          AND NOT rolinherit
          AND rolcanlogin
    ) THEN
        RAISE EXCEPTION 'apdl_agents is missing or privileged';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'apdl_runtime'
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolbypassrls
          AND NOT rolinherit
          AND rolcanlogin
    ) THEN
        RAISE EXCEPTION 'apdl_runtime is missing or privileged';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'apdl_audit_operator'
          AND NOT rolcanlogin
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolbypassrls
          AND NOT rolinherit
    ) THEN
        RAISE EXCEPTION 'apdl_audit_operator is missing or privileged';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles
        WHERE rolname = 'apdl_audit_purge_definer'
          AND NOT rolcanlogin
          AND NOT rolsuper
          AND NOT rolcreatedb
          AND NOT rolcreaterole
          AND NOT rolreplication
          AND NOT rolbypassrls
          AND NOT rolinherit
    ) THEN
        RAISE EXCEPTION 'apdl_audit_purge_definer is missing or privileged';
    END IF;

    SELECT oid
    INTO agents_role_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_agents';
    SELECT oid
    INTO runtime_role_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_runtime';
    SELECT oid
    INTO operator_role_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_audit_operator';
    SELECT oid
    INTO definer_role_oid
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_audit_purge_definer';
    SELECT oid
    INTO current_database_oid
    FROM pg_catalog.pg_database
    WHERE datname = current_database();
    canonical_function_oid := to_regprocedure(
        'public.apdl_purge_experiment_audit(text,timestamptz,text,text)'
    );

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members
        WHERE member IN (
            agents_role_oid,
            runtime_role_oid,
            operator_role_oid,
            definer_role_oid
        )
    ) THEN
        RAISE EXCEPTION 'a fixed APDL role is a member of another role';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database
        WHERE datdba = agents_role_oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace
        WHERE nspowner = agents_role_oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
          AND refobjid = agents_role_oid
          AND deptype = 'o'
    ) THEN
        RAISE EXCEPTION 'apdl_agents owns a database, schema, or object';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members
        WHERE roleid = definer_role_oid
    ) THEN
        RAISE EXCEPTION 'the SECURITY DEFINER role has members';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database
        WHERE datdba = runtime_role_oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace
        WHERE nspowner = runtime_role_oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
          AND refobjid = runtime_role_oid
          AND deptype = 'o'
    ) THEN
        RAISE EXCEPTION 'apdl_runtime owns a database, schema, or object';
    END IF;

    IF pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'INSERT'
    ) OR pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
        'apdl_runtime', 'public.agent_service_capabilities', 'DELETE'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid =
                'public.agent_service_capabilities'::pg_catalog.regclass
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
          AND (
              pg_catalog.has_column_privilege(
                  'apdl_runtime', attribute.attrelid, attribute.attnum, 'INSERT'
              )
              OR pg_catalog.has_column_privilege(
                  'apdl_runtime', attribute.attrelid, attribute.attnum, 'UPDATE'
              )
          )
    ) THEN
        RAISE EXCEPTION 'apdl_runtime can mint or revoke service capabilities';
    END IF;
    IF NOT pg_catalog.has_table_privilege(
        'apdl_runtime',
        'public.agent_service_capabilities',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'apdl_runtime cannot validate service capabilities';
    END IF;
    IF NOT pg_catalog.has_table_privilege(
        'apdl_agents',
        'public.agent_service_capabilities',
        'SELECT'
    ) OR NOT pg_catalog.has_table_privilege(
        'apdl_agents',
        'public.agent_service_capabilities',
        'DELETE'
    ) THEN
        RAISE EXCEPTION 'apdl_agents cannot manage service capabilities';
    END IF;
    IF pg_catalog.has_table_privilege(
        'apdl_agents',
        'public.agent_service_capabilities',
        'UPDATE'
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents',
        'public.agent_service_capabilities',
        'INSERT'
    ) THEN
        RAISE EXCEPTION 'apdl_agents has broad capability mutation authority';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
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
        ]::TEXT[]) AS required(column_name)
        WHERE NOT pg_catalog.has_column_privilege(
            'apdl_agents',
            'public.agent_service_capabilities',
            required.column_name,
            'INSERT'
        )
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'capability_id', 'issued_at', 'consumed_at'
        ]::TEXT[]) AS forbidden(column_name)
        WHERE pg_catalog.has_column_privilege(
            'apdl_agents',
            'public.agent_service_capabilities',
            forbidden.column_name,
            'INSERT'
        )
    ) THEN
        RAISE EXCEPTION 'apdl_agents capability insert columns are not exact';
    END IF;

    IF canonical_function_oid IS NULL OR (
        SELECT count(*)
        FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = 'public'
          AND procedure.proname = 'apdl_purge_experiment_audit'
    ) <> 1 THEN
        RAISE EXCEPTION 'canonical purge function is missing or overloaded';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS procedure
        WHERE procedure.oid = canonical_function_oid
          AND procedure.proowner = definer_role_oid
          AND procedure.prosecdef
          AND procedure.prokind = 'f'
          AND procedure.prolang = (
              SELECT oid
              FROM pg_catalog.pg_language
              WHERE lanname = 'plpgsql'
          )
          AND procedure.prorettype = 'pg_catalog.int8'::pg_catalog.regtype
          AND procedure.proconfig IS NOT DISTINCT FROM
              ARRAY['search_path=pg_catalog']::TEXT[]
    ) THEN
        RAISE EXCEPTION 'canonical purge function definition is invalid';
    END IF;

    IF (
        SELECT
            count(*) = 2
            AND count(*) FILTER (
                WHERE acl.grantee = definer_role_oid
                  AND acl.privilege_type = 'EXECUTE'
                  AND NOT acl.is_grantable
            ) = 1
            AND count(*) FILTER (
                WHERE acl.grantee = operator_role_oid
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
        WHERE procedure.oid = canonical_function_oid
    ) IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'canonical purge function ACL is not exact';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend AS dependency
        WHERE dependency.refclassid =
              'pg_catalog.pg_authid'::pg_catalog.regclass
          AND dependency.refobjid = definer_role_oid
          AND dependency.deptype = 'o'
          AND NOT (
              dependency.dbid = current_database_oid
              AND dependency.classid =
                  'pg_catalog.pg_proc'::pg_catalog.regclass
              AND dependency.objid = canonical_function_oid
              AND dependency.objsubid = 0
          )
    ) THEN
        RAISE EXCEPTION 'purge definer owns an object other than its function';
    END IF;

    IF has_schema_privilege(
        'apdl_runtime',
        'public',
        'CREATE'
    ) OR has_schema_privilege(
        'apdl_audit_operator',
        'public',
        'CREATE'
    ) OR has_schema_privilege(
        'apdl_audit_purge_definer',
        'public',
        'CREATE'
    ) THEN
        RAISE EXCEPTION 'a fixed APDL role can create objects in public';
    END IF;
    IF NOT has_schema_privilege(
        'apdl_audit_operator',
        'public',
        'USAGE'
    ) OR NOT has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_log',
        'SELECT'
    ) OR NOT has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_purge_log',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'operator caller cannot preview and verify a purge';
    END IF;
    IF has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_log',
        'INSERT'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_log',
        'UPDATE'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_log',
        'DELETE'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_log',
        'TRUNCATE'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_purge_log',
        'INSERT'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_purge_log',
        'UPDATE'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_purge_log',
        'DELETE'
    ) OR has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_purge_log',
        'TRUNCATE'
    ) THEN
        RAISE EXCEPTION 'operator caller has direct audit mutation privileges';
    END IF;
    IF (
        SELECT pg_get_userbyid(proowner)
        FROM pg_catalog.pg_proc
        WHERE oid = canonical_function_oid
    ) <> 'apdl_audit_purge_definer' THEN
        RAISE EXCEPTION 'purge function is not owned by the NOLOGIN definer';
    END IF;
    IF has_table_privilege(
        'apdl_audit_operator',
        'public.experiment_audit_log',
        'DELETE'
    ) THEN
        RAISE EXCEPTION 'operator caller has direct audit DELETE';
    END IF;
    IF NOT has_table_privilege(
        'apdl_audit_purge_definer',
        'public.experiment_audit_log',
        'DELETE'
    ) THEN
        RAISE EXCEPTION 'purge definer lacks its narrow DELETE privilege';
    END IF;
    IF NOT has_column_privilege(
        'apdl_audit_purge_definer',
        'public.experiment_audit_log',
        'project_id',
        'SELECT'
    ) OR NOT has_column_privilege(
        'apdl_audit_purge_definer',
        'public.experiment_audit_log',
        'created_at',
        'SELECT'
    ) THEN
        RAISE EXCEPTION 'purge definer cannot read its bounded DELETE predicate';
    END IF;
END
$assert_roles$;

DO $assert_service_boundaries$
DECLARE
    capability_consumer_oid OID;
    capability_function_oid OID;
    project_authority_oid OID;
    project_authority_function_oids OID[];
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
        RAISE EXCEPTION 'the seven fixed APDL roles are not present';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY (ARRAY[
            'apdl_runtime', 'apdl_agents', 'apdl_llm_vault'
        ]::TEXT[])
          AND (
              NOT role.rolcanlogin OR role.rolsuper OR role.rolcreatedb
              OR role.rolcreaterole OR role.rolreplication
              OR role.rolbypassrls OR role.rolinherit
          )
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = ANY (ARRAY[
            'apdl_audit_operator',
            'apdl_audit_purge_definer',
            'apdl_project_authority_definer',
            'apdl_capability_consumer_definer'
        ]::TEXT[])
          AND (
              role.rolcanlogin OR role.rolsuper OR role.rolcreatedb
              OR role.rolcreaterole OR role.rolreplication
              OR role.rolbypassrls OR role.rolinherit
          )
    ) THEN
        RAISE EXCEPTION 'a fixed APDL role has an unsafe role posture';
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
        RAISE EXCEPTION 'a fixed APDL role participates in role membership';
    END IF;

    IF EXISTS (
        WITH expected(table_name, privilege_type) AS (
            VALUES
                ('agent_approval_commands', 'INSERT'),
                ('agent_approval_commands', 'UPDATE'),
                ('agent_approval_effects', 'INSERT'),
                ('agent_approval_effects', 'UPDATE'),
                ('agent_run_results', 'INSERT'),
                ('agent_run_results', 'UPDATE'),
                ('agent_runs', 'INSERT'),
                ('agent_runs', 'UPDATE'),
                ('custom_agent_test_runs', 'INSERT'),
                ('custom_agent_test_runs', 'UPDATE'),
                ('custom_agents', 'INSERT'),
                ('custom_agents', 'UPDATE'),
                ('designed_experiments', 'INSERT'),
                ('designed_experiments', 'UPDATE'),
                ('experiment_verdicts', 'INSERT'),
                ('experiment_verdicts', 'UPDATE'),
                ('feature_proposals', 'INSERT'),
                ('feature_proposals', 'UPDATE'),
                ('llm_calls', 'INSERT'),
                ('llm_calls', 'UPDATE'),
                ('llm_provider_attempts', 'INSERT'),
                ('llm_provider_attempts', 'UPDATE'),
                ('llm_project_policies', 'UPDATE'),
                ('agent_memory', 'INSERT'),
                ('agent_memory', 'DELETE'),
                ('llm_project_model_assignments', 'INSERT'),
                ('llm_project_model_assignments', 'DELETE'),
                ('llm_project_provider_policies', 'INSERT'),
                ('llm_project_provider_policies', 'DELETE'),
                ('agent_service_capabilities', 'DELETE'),
                ('agent_approval_decisions', 'INSERT'),
                ('agent_audit_log', 'INSERT'),
                ('agent_mutation_quota_reservations', 'INSERT'),
                ('llm_project_setup_audit', 'INSERT')
        ), actual AS (
            SELECT class.relname::TEXT AS table_name,
                   privilege.privilege_type
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            CROSS JOIN unnest(
                ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']::TEXT[]
            ) AS privilege(privilege_type)
            WHERE namespace.nspname = 'public'
              AND class.relkind IN ('r', 'p')
              AND pg_catalog.has_table_privilege(
                  'apdl_agents', class.oid, privilege.privilege_type
              )
        )
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) THEN
        RAISE EXCEPTION 'apdl_agents table DML allowlist is not exact';
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
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents', 'public.agent_service_capabilities', 'INSERT'
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents', 'public.agent_service_capabilities', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'apdl_agents capability INSERT grant is not column-only';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS sequence
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = sequence.relnamespace
        WHERE namespace.nspname = 'public'
          AND sequence.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(
                  'apdl_agents', sequence.oid, 'USAGE'
              ) OR pg_catalog.has_sequence_privilege(
                  'apdl_agents', sequence.oid, 'SELECT'
              ) OR pg_catalog.has_sequence_privilege(
                  'apdl_agents', sequence.oid, 'UPDATE'
              )
          )
          AND sequence.relname <> ALL (ARRAY[
              'agent_audit_log_id_seq',
              'agent_memory_id_seq',
              'experiment_verdicts_id_seq'
          ]::NAME[])
    ) OR EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'agent_audit_log_id_seq',
            'agent_memory_id_seq',
            'experiment_verdicts_id_seq'
        ]::TEXT[]) AS expected(sequence_name)
        WHERE NOT pg_catalog.has_sequence_privilege(
            'apdl_agents', 'public.' || expected.sequence_name, 'USAGE'
        ) OR NOT pg_catalog.has_sequence_privilege(
            'apdl_agents', 'public.' || expected.sequence_name, 'SELECT'
        ) OR pg_catalog.has_sequence_privilege(
            'apdl_agents', 'public.' || expected.sequence_name, 'UPDATE'
        )
    ) THEN
        RAISE EXCEPTION 'apdl_agents sequence allowlist is not exact';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM unnest(ARRAY[
            'admin_credential_audit',
            'admin_login_account_risk',
            'admin_login_rate_buckets',
            'admin_login_source_risk',
            'admin_managed_credentials',
            'admin_project_invitations',
            'admin_project_membership_audit',
            'admin_project_ownership_audit',
            'admin_proxy_audit',
            'admin_security_notifications',
            'admin_sessions',
            'llm_vault_access_audit',
            'llm_vault_audit',
            'llm_vault_connections',
            'llm_vault_key_rotation_audit',
            'llm_vault_provider_models',
            'llm_vault_provider_secrets'
        ]::TEXT[]) AS sensitive(table_name)
        WHERE pg_catalog.has_table_privilege(
            'apdl_agents', 'public.' || sensitive.table_name,
            'SELECT, INSERT, UPDATE, DELETE, TRUNCATE'
        )
    ) OR pg_catalog.has_column_privilege(
        'apdl_agents', 'public.admin_users', 'password_hash', 'SELECT'
    ) OR pg_catalog.has_column_privilege(
        'apdl_agents', 'public.admin_users', 'created_at', 'SELECT'
    ) OR pg_catalog.has_column_privilege(
        'apdl_agents', 'public.admin_users', 'updated_at', 'SELECT'
    ) OR pg_catalog.has_column_privilege(
        'apdl_agents', 'public.admin_user_projects', 'created_at', 'SELECT'
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents', 'public.admin_user_projects', 'UPDATE'
    ) OR pg_catalog.has_table_privilege(
        'apdl_agents', 'public.admin_project_membership_audit', 'INSERT'
    ) THEN
        RAISE EXCEPTION 'apdl_agents can access sensitive Admin or vault state';
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
        RAISE EXCEPTION 'capability consume function definition is invalid';
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
                   SELECT oid FROM pg_catalog.pg_roles
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
        RAISE EXCEPTION 'capability consume function ACL is not exact';
    END IF;

    IF EXISTS (
        WITH expected(table_name, privilege_type) AS (
            VALUES ('agent_service_capabilities', 'SELECT')
        ), actual AS (
            SELECT class.relname::TEXT, acl.privilege_type
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(class.relacl) AS acl
            WHERE namespace.nspname = 'public'
              AND class.relkind IN ('r', 'p')
              AND acl.grantee = capability_consumer_oid
        )
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) OR EXISTS (
        WITH expected(table_name, column_name, privilege_type) AS (
            VALUES
                ('agent_service_capabilities', 'consumed_at', 'UPDATE'),
                ('agent_approval_effects', 'effect_id', 'SELECT'),
                ('agent_approval_effects', 'run_id', 'SELECT'),
                ('agent_approval_effects', 'project_id', 'SELECT'),
                ('agent_approval_effects', 'status', 'SELECT'),
                ('agent_approval_effects', 'lease_owner_id', 'SELECT'),
                ('agent_approval_effects', 'lease_expires_at', 'SELECT'),
                ('agent_runs', 'run_id', 'SELECT'),
                ('agent_runs', 'project_id', 'SELECT'),
                ('agent_runs', 'execution_lane_project_id', 'SELECT'),
                ('agent_runs', 'status', 'SELECT')
        ), actual AS (
            SELECT class.relname::TEXT, attribute.attname::TEXT,
                   acl.privilege_type
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS class
              ON class.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            WHERE namespace.nspname = 'public'
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
              AND acl.grantee = capability_consumer_oid
        )
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) THEN
        RAISE EXCEPTION 'capability consumer table dependencies are not exact';
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
           JOIN pg_catalog.pg_proc AS procedure
             ON procedure.oid = expected.oid
           WHERE procedure.proowner <> project_authority_oid
              OR NOT procedure.prosecdef
              OR procedure.proconfig IS DISTINCT FROM
                  ARRAY['search_path=pg_catalog, public']::TEXT[]
       ) THEN
        RAISE EXCEPTION 'project authority function ownership is invalid';
    END IF;

    IF EXISTS (
        WITH expected(function_oid, grantee_name) AS (
            VALUES
                (project_authority_function_oids[1],
                 'apdl_project_authority_definer'),
                (project_authority_function_oids[1], 'apdl_agents'),
                (project_authority_function_oids[1], 'apdl_llm_vault'),
                (project_authority_function_oids[2],
                 'apdl_project_authority_definer'),
                (project_authority_function_oids[2], 'apdl_agents'),
                (project_authority_function_oids[3],
                 'apdl_project_authority_definer'),
                (project_authority_function_oids[3], 'apdl_runtime'),
                (project_authority_function_oids[3], 'apdl_agents')
        ), actual AS (
            SELECT procedure.oid, role.rolname::TEXT
            FROM pg_catalog.pg_proc AS procedure
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(
                    procedure.proacl,
                    pg_catalog.acldefault('f', procedure.proowner)
                )
            ) AS acl
            JOIN pg_catalog.pg_roles AS role ON role.oid = acl.grantee
            WHERE procedure.oid = ANY (project_authority_function_oids)
              AND acl.privilege_type = 'EXECUTE'
              AND NOT acl.is_grantable
        )
        (SELECT * FROM actual EXCEPT SELECT * FROM expected)
        UNION ALL
        (SELECT * FROM expected EXCEPT SELECT * FROM actual)
    ) THEN
        RAISE EXCEPTION 'project authority function ACLs are not exact';
    END IF;
END
$assert_service_boundaries$;

SELECT 'DROP ROLE apdl_audit_privilege_test'
WHERE EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_audit_privilege_test'
)
\gexec
CREATE ROLE apdl_audit_privilege_test WITH
    LOGIN
    INHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD :'test_actor_password';
GRANT apdl_audit_operator TO apdl_audit_privilege_test;
SQL

runtime_posture="$(
    runtime_psql -c \
        "SELECT session_user,
                current_user,
                rolsuper,
                pg_has_role(session_user, '$operator_role', 'MEMBER'),
                pg_has_role(session_user, '$definer_role', 'MEMBER'),
                (
                    SELECT pg_get_userbyid(datdba) = session_user
                    FROM pg_catalog.pg_database
                    WHERE datname = current_database()
                ),
                (
                    SELECT pg_get_userbyid(relowner) = session_user
                    FROM pg_catalog.pg_class
                    WHERE oid = 'public.experiment_audit_log'::regclass
                )
         FROM pg_catalog.pg_roles
         WHERE rolname = session_user"
)"
if [ "$runtime_posture" != "apdl_runtime|apdl_runtime|f|f|f|f|f" ]; then
    echo "Unexpected apdl_runtime privilege posture: $runtime_posture" >&2
    exit 1
fi

capability_id="$(
    owner_psql --set=project_a="$project_a" <<'SQL'
BEGIN;
SET LOCAL session_replication_role = replica;

INSERT INTO public.admin_projects (project_id)
VALUES (:'project_a');

INSERT INTO public.agent_runs (
    run_id, project_id, trigger_type, status, phase
)
VALUES (
    'capability-proof-run', :'project_a', 'manual',
    'approval_queued', 'code_implementation_approval'
);

WITH command AS (
    INSERT INTO public.agent_approval_commands (
        command_id,
        run_id,
        project_id,
        actor_credential_id,
        request_sha256,
        gate_id,
        gate_agent,
        status,
        resume_status,
        approved_count,
        rejected_count
    )
    VALUES (
        gen_random_uuid(),
        'capability-proof-run',
        :'project_a',
        'test-agents',
        repeat('1', 64),
        'capability-proof-run:code_implementation',
        'code_implementation',
        'processing',
        'approved',
        1,
        0
    )
    RETURNING command_id, run_id, project_id
), effect AS (
    INSERT INTO public.agent_approval_effects (
        effect_id,
        command_id,
        run_id,
        project_id,
        item_id,
        effect_type,
        effect_order,
        payload,
        status,
        idempotency_key,
        lease_owner_id,
        lease_expires_at
    )
    SELECT
        gen_random_uuid(),
        command.command_id,
        command.run_id,
        command.project_id,
        'capability-proof-item',
        'record_proposal_rejection',
        0,
        '{}'::JSONB,
        'processing',
        'capability-proof-effect',
        'capability-proof-lease',
        statement_timestamp() + interval '2 minutes'
    FROM command
    RETURNING effect_id, run_id, project_id
), capability AS (
    INSERT INTO public.agent_service_capabilities (
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
    )
    SELECT
        repeat('2', 64),
        effect.project_id,
        'approval_effect',
        effect.effect_id::TEXT,
        effect.run_id,
        'capability-proof-lease',
        ARRAY['config']::TEXT[],
        ARRAY['config:write']::TEXT[],
        repeat('3', 64),
        statement_timestamp() + interval '1 minute'
    FROM effect
    RETURNING capability_id
)
SELECT capability_id FROM capability;
COMMIT;
SQL
)"
if [ -z "$capability_id" ]; then
    echo "Failed to create the capability consume proof fixture" >&2
    exit 1
fi

expect_failure runtime \
    "UPDATE public.agent_service_capabilities
     SET consumed_at = statement_timestamp()
     WHERE capability_id = '$capability_id'"

first_consume="$(
    runtime_psql -c \
        "SELECT public.apdl_consume_agent_service_capability(
            '$capability_id',
            repeat('2', 64),
            'config',
            'config:write',
            repeat('3', 64)
        )"
)"
second_consume="$(
    runtime_psql -c \
        "SELECT public.apdl_consume_agent_service_capability(
            '$capability_id',
            repeat('2', 64),
            'config',
            'config:write',
            repeat('3', 64)
        )"
)"
consumed_state="$(
    runtime_psql -c \
        "SELECT consumed_at IS NOT NULL
         FROM public.agent_service_capabilities
         WHERE capability_id = '$capability_id'"
)"
if [ "$first_consume|$second_consume|$consumed_state" != "t|f|t" ]; then
    echo "Capability consumption was not one-shot: $first_consume|$second_consume|$consumed_state" >&2
    exit 1
fi

runtime_psql \
    --set=project_a="$project_a" \
    --set=project_b="$project_b" <<'SQL'
INSERT INTO public.experiment_audit_log (
    project_id,
    experiment_key,
    action,
    actor,
    after,
    created_at
)
VALUES
    (
        :'project_a',
        'operator-proof-a',
        'experiment_created',
        'system:operator-boundary-proof',
        '{}'::jsonb,
        now() - interval '2 days'
    ),
    (
        :'project_b',
        'operator-proof-b',
        'experiment_created',
        'system:operator-boundary-proof',
        '{}'::jsonb,
        now() - interval '2 days'
    );
SQL

preview_state="$(
    operator_psql -c \
        "SELECT
             count(*) FILTER (WHERE project_id = '$project_a'),
             count(*) FILTER (WHERE project_id = '$project_b'),
             (
                 SELECT count(*)
                 FROM public.experiment_audit_purge_log
                 WHERE project_id IN ('$project_a', '$project_b')
             )
         FROM public.experiment_audit_log
         WHERE project_id IN ('$project_a', '$project_b')"
)"
if [ "$preview_state" != "1|1|0" ]; then
    echo "Operator could not preview the bounded purge: $preview_state" >&2
    exit 1
fi

expect_failure runtime \
    "DELETE FROM public.experiment_audit_log WHERE project_id = '$project_a'"
expect_failure runtime \
    "SELECT public.apdl_purge_experiment_audit(
        '$project_a',
        now(),
        '$reason',
        '$confirmation'
    )"
expect_failure operator \
    "DELETE FROM public.experiment_audit_log WHERE project_id = '$project_a'"
expect_failure owner \
    "DELETE FROM public.experiment_audit_log WHERE project_id = '$project_b'" \
    "experiment lifecycle audit rows are immutable"
expect_failure operator \
    "SELECT public.apdl_purge_experiment_audit(
        '$project_a',
        now(),
        '$reason',
        'PURGE EXPERIMENT AUDIT NOW'
    )" \
    "exact purge confirmation is required"

# A successful SECURITY DEFINER call remains fully transactional. Force a
# later statement to abort the connection's transaction; disconnect rollback
# must restore both the project row and its purge evidence.
expect_failure operator \
    "BEGIN;
     SELECT public.apdl_purge_experiment_audit(
         '$project_a',
         now(),
         '$reason',
         '$confirmation'
     );
     DO \$forced_failure\$
     BEGIN
         RAISE EXCEPTION 'forced post-purge transaction failure';
     END
     \$forced_failure\$;
     COMMIT;" \
    "forced post-purge transaction failure"

rollback_state="$(
    owner_psql -c \
        "SELECT
             count(*) FILTER (WHERE project_id = '$project_a'),
             count(*) FILTER (WHERE project_id = '$project_b'),
             (
                 SELECT count(*)
                 FROM public.experiment_audit_purge_log
                 WHERE project_id IN ('$project_a', '$project_b')
             ),
             (
                 SELECT tgenabled = 'O'
                 FROM pg_catalog.pg_trigger
                 WHERE tgrelid = 'public.experiment_audit_log'::regclass
                   AND tgname = 'experiment_audit_log_no_update_delete'
                   AND NOT tgisinternal
             )
         FROM public.experiment_audit_log
         WHERE project_id IN ('$project_a', '$project_b')"
)"
if [ "$rollback_state" != "1|1|0|t" ]; then
    echo "Operator purge did not roll back atomically: $rollback_state" >&2
    exit 1
fi

deleted_rows="$(
    operator_psql -c \
        "SELECT public.apdl_purge_experiment_audit(
            '$project_a',
            now(),
            '$reason',
            '$confirmation'
        )"
)"
if [ "$deleted_rows" != "1" ]; then
    echo "Operator purge deleted an unexpected row count: $deleted_rows" >&2
    exit 1
fi

purge_state="$(
    operator_psql -c \
        "SELECT
             count(*) FILTER (WHERE project_id = '$project_a'),
             count(*) FILTER (WHERE project_id = '$project_b'),
             (
                 SELECT count(*)
                 FROM public.experiment_audit_purge_log
                 WHERE project_id = '$project_a'
                   AND deleted_rows = 1
                   AND actor = '$test_actor'
                   AND reason = '$reason'
             )
         FROM public.experiment_audit_log
         WHERE project_id IN ('$project_a', '$project_b')"
)"
if [ "$purge_state" != "0|1|1" ]; then
    echo "Operator purge violated project/audit boundaries: $purge_state" >&2
    exit 1
fi

expect_failure owner \
    "UPDATE public.experiment_audit_purge_log
     SET reason = 'tampered'
     WHERE project_id = '$project_a'"
expect_failure owner \
    "DELETE FROM public.experiment_audit_purge_log
     WHERE project_id = '$project_a'"
expect_failure owner "TRUNCATE public.experiment_audit_purge_log"

echo "PostgreSQL runtime/operator privilege boundary passed"
