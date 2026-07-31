#!/usr/bin/env bash
# Fresh-cluster bootstrap for the dedicated APDL database identities.
#
# The PostgreSQL image runs this file only while initdb is creating a new data
# directory. Schema/object grants remain migration-owned; this bootstrap only
# establishes the cluster roles before the canonical migration sequence runs.

set -Eeuo pipefail

runtime_password="${APDL_RUNTIME_POSTGRES_PASSWORD:?APDL_RUNTIME_POSTGRES_PASSWORD is required}"
llm_vault_password="${APDL_LLM_VAULT_POSTGRES_PASSWORD:?APDL_LLM_VAULT_POSTGRES_PASSWORD is required}"
postgres_user="${POSTGRES_USER:-apdl}"
postgres_db="${POSTGRES_DB:-$postgres_user}"

if [ "$postgres_user" = "apdl_runtime" ] \
    || [ "$postgres_user" = "apdl_audit_operator" ] \
    || [ "$postgres_user" = "apdl_audit_purge_definer" ] \
    || [ "$postgres_user" = "apdl_llm_vault" ]; then
    echo "POSTGRES_USER must be a distinct migration owner" >&2
    exit 1
fi

# Compose interpolates this value into a PostgreSQL URL. Reject delimiters
# instead of letting the bootstrap password and the service URL decode to
# different credentials.
if [[ ! "$runtime_password" =~ ^[A-Za-z0-9._~-]{16,128}$ ]]; then
    echo "APDL_RUNTIME_POSTGRES_PASSWORD must be 16-128 URI-unreserved characters" >&2
    exit 1
fi
if [[ ! "$llm_vault_password" =~ ^[A-Za-z0-9._~-]{16,128}$ ]]; then
    echo "APDL_LLM_VAULT_POSTGRES_PASSWORD must be 16-128 URI-unreserved characters" >&2
    exit 1
fi

PGPASSWORD="${POSTGRES_PASSWORD:-${PGPASSWORD:-}}" psql \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --set=runtime_password="$runtime_password" \
    --set=llm_vault_password="$llm_vault_password" \
    --username "$postgres_user" \
    --dbname "$postgres_db" <<'SQL'
BEGIN;

SELECT format(
    'CREATE ROLE apdl_runtime LOGIN PASSWORD %L',
    :'runtime_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'apdl_runtime'
)
\gexec

ALTER ROLE apdl_runtime WITH
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD :'runtime_password';

SELECT format(
    'CREATE ROLE apdl_llm_vault LOGIN PASSWORD %L',
    :'llm_vault_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'apdl_llm_vault'
)
\gexec

ALTER ROLE apdl_llm_vault WITH
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS
    PASSWORD :'llm_vault_password';

SELECT
    'CREATE ROLE apdl_audit_operator NOLOGIN'
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'apdl_audit_operator'
)
\gexec

ALTER ROLE apdl_audit_operator WITH
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

SELECT
    'CREATE ROLE apdl_audit_purge_definer NOLOGIN'
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_audit_purge_definer'
)
\gexec

ALTER ROLE apdl_audit_purge_definer WITH
    NOLOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOINHERIT
    NOREPLICATION
    NOBYPASSRLS;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public
    FROM apdl_runtime, apdl_llm_vault, apdl_audit_operator,
         apdl_audit_purge_definer;

DO $apdl_validate_fixed_roles$
DECLARE
    role_record RECORD;
BEGIN
    FOR role_record IN
        SELECT oid, rolname
        FROM pg_catalog.pg_roles
        WHERE rolname IN (
            'apdl_runtime',
            'apdl_llm_vault',
            'apdl_audit_operator',
            'apdl_audit_purge_definer'
        )
    LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_auth_members
            WHERE member = role_record.oid
        ) THEN
            RAISE EXCEPTION
                '% must not be a member of any database role',
                role_record.rolname;
        END IF;
        IF pg_catalog.has_schema_privilege(
            role_record.rolname,
            'public',
            'CREATE'
        ) THEN
            RAISE EXCEPTION
                '% must not have effective CREATE on schema public',
                role_record.rolname;
        END IF;
    END LOOP;

    SELECT oid, rolname
    INTO role_record
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_runtime';
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database
        WHERE datdba = role_record.oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace
        WHERE nspowner = role_record.oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
          AND refobjid = role_record.oid
          AND deptype = 'o'
    ) THEN
        RAISE EXCEPTION
            'apdl_runtime must not own a database, schema, or database object';
    END IF;

    SELECT oid, rolname
    INTO role_record
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_llm_vault';
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_database
        WHERE datdba = role_record.oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace
        WHERE nspowner = role_record.oid
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
          AND refobjid = role_record.oid
          AND deptype = 'o'
    ) THEN
        RAISE EXCEPTION
            'apdl_llm_vault must not own a database, schema, or database object';
    END IF;

    SELECT oid, rolname
    INTO role_record
    FROM pg_catalog.pg_roles
    WHERE rolname = 'apdl_audit_purge_definer';
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members
        WHERE roleid = role_record.oid
    ) THEN
        RAISE EXCEPTION
            'apdl_audit_purge_definer must not be granted to any database role';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_shdepend
        WHERE refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
          AND refobjid = role_record.oid
          AND deptype = 'o'
    ) THEN
        RAISE EXCEPTION
            'apdl_audit_purge_definer must not own preexisting database objects';
    END IF;
END
$apdl_validate_fixed_roles$;

COMMIT;
SQL
