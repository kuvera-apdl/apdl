-- Migration 008: Separate tenant Codegen preferences from platform safety.
--
-- The legacy `policy` document was tenant-writable and was also consumed as a
-- safety authority. Preserve only preferences that cannot weaken the built-in
-- 50-file / 2,000-line ceilings, discard allowlists and workflow paths, and move
-- to one strict, versioned tenant contract.

ALTER TABLE codegen_connections ADD COLUMN IF NOT EXISTS tenant_policy JSONB;

-- Reject malformed or ambiguous legacy documents with the affected project id
-- instead of guessing at a migration. Fields that explicitly weakened safety
-- (large ceilings and allowlists) are well understood and are safely discarded
-- below rather than carried into the new contract.
DO $validate_legacy_codegen_policy$
DECLARE
    legacy RECORD;
    gates JSONB;
    runtime_request JSONB;
    protected JSONB;
BEGIN
    FOR legacy IN
        SELECT project_id, policy
        FROM codegen_connections
        WHERE tenant_policy IS NULL
    LOOP
        IF jsonb_typeof(legacy.policy) <> 'object' THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: policy must be an object',
                legacy.project_id;
        END IF;
        IF EXISTS (
            SELECT 1 FROM jsonb_object_keys(legacy.policy) AS item(key)
            WHERE item.key NOT IN ('test_cmd', 'gates', 'runtime_acceptance')
        ) THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: unknown top-level fields',
                legacy.project_id;
        END IF;
        IF legacy.policy ? 'test_cmd' AND (
            jsonb_typeof(legacy.policy->'test_cmd') NOT IN ('string', 'null')
            OR (
                jsonb_typeof(legacy.policy->'test_cmd') = 'string'
                AND (
                    length(legacy.policy->>'test_cmd') NOT BETWEEN 1 AND 1000
                    OR btrim(legacy.policy->>'test_cmd') = ''
                    OR position(E'\n' IN legacy.policy->>'test_cmd') > 0
                    OR position(E'\r' IN legacy.policy->>'test_cmd') > 0
                )
            )
        ) THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: test_cmd must be a string or null',
                legacy.project_id;
        END IF;

        gates := legacy.policy->'gates';
        IF gates IS NOT NULL AND jsonb_typeof(gates) NOT IN ('object', 'null') THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: gates must be an object',
                legacy.project_id;
        END IF;
        IF jsonb_typeof(gates) = 'object' AND EXISTS (
            SELECT 1 FROM jsonb_object_keys(gates) AS item(key)
            WHERE item.key NOT IN (
                'max_files', 'max_lines', 'protected_paths',
                'allowed_protected_paths'
            )
        ) THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: unknown gates fields',
                legacy.project_id;
        END IF;
        IF jsonb_typeof(gates) = 'object' THEN
            IF gates ? 'max_files' AND (
                jsonb_typeof(gates->'max_files') <> 'number'
                OR COALESCE(gates->>'max_files', '') !~ '^[1-9][0-9]*$'
            ) THEN
                RAISE EXCEPTION
                    'Cannot migrate Codegen policy for project %: max_files must be a positive integer',
                    legacy.project_id;
            END IF;
            IF gates ? 'max_lines' AND (
                jsonb_typeof(gates->'max_lines') <> 'number'
                OR COALESCE(gates->>'max_lines', '') !~ '^[1-9][0-9]*$'
            ) THEN
                RAISE EXCEPTION
                    'Cannot migrate Codegen policy for project %: max_lines must be a positive integer',
                    legacy.project_id;
            END IF;
            IF gates ? 'protected_paths' THEN
                protected := gates->'protected_paths';
                IF jsonb_typeof(protected) <> 'array' THEN
                    RAISE EXCEPTION
                        'Cannot migrate Codegen policy for project %: protected_paths must be an array',
                        legacy.project_id;
                END IF;
                IF jsonb_array_length(protected) > 64 OR EXISTS (
                       SELECT 1
                       FROM jsonb_array_elements(protected) AS item(value)
                       WHERE jsonb_typeof(item.value) <> 'string'
                          OR length(item.value #>> '{}') NOT BETWEEN 1 AND 256
                          OR (item.value #>> '{}') LIKE '/%'
                          OR (item.value #>> '{}') LIKE './%'
                          OR position(E'\\' IN (item.value #>> '{}')) > 0
                          OR '..' = ANY(string_to_array(item.value #>> '{}', '/'))
                          OR position(E'\n' IN (item.value #>> '{}')) > 0
                          OR position(E'\r' IN (item.value #>> '{}')) > 0
                   ) THEN
                    RAISE EXCEPTION
                        'Cannot migrate Codegen policy for project %: protected_paths must contain at most 64 canonical paths',
                        legacy.project_id;
                END IF;
            END IF;
        END IF;

        runtime_request := legacy.policy->'runtime_acceptance';
        IF runtime_request IS NOT NULL
           AND jsonb_typeof(runtime_request) NOT IN ('object', 'null') THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: runtime_acceptance must be an object',
                legacy.project_id;
        END IF;
        IF jsonb_typeof(runtime_request) = 'object' AND EXISTS (
            SELECT 1 FROM jsonb_object_keys(runtime_request) AS item(key)
            WHERE item.key NOT IN (
                'schema_version', 'workflow_changes_authorized',
                'generated_workflow_path'
            )
        ) THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: unknown runtime_acceptance fields',
                legacy.project_id;
        END IF;
        IF jsonb_typeof(runtime_request) = 'object'
           AND runtime_request ? 'schema_version'
           AND runtime_request->>'schema_version' <> 'runtime_acceptance_policy@1' THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: invalid runtime_acceptance schema version',
                legacy.project_id;
        END IF;
        IF jsonb_typeof(runtime_request) = 'object'
           AND runtime_request ? 'workflow_changes_authorized'
           AND jsonb_typeof(runtime_request->'workflow_changes_authorized') <> 'boolean' THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: workflow_changes_authorized must be boolean',
                legacy.project_id;
        END IF;
        IF jsonb_typeof(runtime_request) = 'object'
           AND runtime_request ? 'generated_workflow_path'
           AND jsonb_typeof(runtime_request->'generated_workflow_path') <> 'string' THEN
            RAISE EXCEPTION
                'Cannot migrate Codegen policy for project %: generated_workflow_path must be a string',
                legacy.project_id;
        END IF;
    END LOOP;
END
$validate_legacy_codegen_policy$;

UPDATE codegen_connections AS connection
SET tenant_policy = jsonb_build_object(
    'schema_version', 'tenant_codegen_connection_policy@1',
    'test_cmd', CASE
        WHEN jsonb_typeof(connection.policy->'test_cmd') = 'string'
        THEN connection.policy->'test_cmd'
        ELSE 'null'::jsonb
    END,
    'gates', jsonb_build_object(
        'max_files', CASE
            WHEN COALESCE(connection.policy->'gates'->>'max_files', '')
                    ~ '^[1-9][0-9]*$'
             AND (connection.policy->'gates'->>'max_files')::numeric <= 50
            THEN connection.policy->'gates'->'max_files'
            ELSE 'null'::jsonb
        END,
        'max_lines', CASE
            WHEN COALESCE(connection.policy->'gates'->>'max_lines', '')
                    ~ '^[1-9][0-9]*$'
             AND (connection.policy->'gates'->>'max_lines')::numeric <= 2000
            THEN connection.policy->'gates'->'max_lines'
            ELSE 'null'::jsonb
        END,
        'additional_protected_paths', COALESCE((
            SELECT jsonb_agg(path ORDER BY path)
            FROM (
                SELECT DISTINCT item.value #>> '{}' AS path
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(
                            connection.policy->'gates'->'protected_paths'
                        ) = 'array'
                        THEN connection.policy->'gates'->'protected_paths'
                        ELSE '[]'::jsonb
                    END
                ) AS item(value)
            ) AS paths
        ), '[]'::jsonb)
    ),
    'runtime_acceptance', jsonb_build_object(
        'schema_version', 'runtime_acceptance_request@1',
        'enabled', CASE
            WHEN jsonb_typeof(
                connection.policy->'runtime_acceptance'
                    ->'workflow_changes_authorized'
             ) = 'boolean'
             AND (
                NOT (
                    connection.policy->'runtime_acceptance'
                        ? 'generated_workflow_path'
                )
                OR connection.policy->'runtime_acceptance'
                    ->>'generated_workflow_path'
                    = '.github/workflows/apdl-runtime-acceptance.yml'
             )
            THEN (
                connection.policy->'runtime_acceptance'
                    ->>'workflow_changes_authorized'
            )::boolean
            ELSE false
        END
    )
)
WHERE tenant_policy IS NULL;

ALTER TABLE codegen_connections
    ALTER COLUMN tenant_policy SET DEFAULT
        '{
          "schema_version":"tenant_codegen_connection_policy@1",
          "test_cmd":null,
          "gates":{
            "max_files":null,
            "max_lines":null,
            "additional_protected_paths":[]
          },
          "runtime_acceptance":{
            "schema_version":"runtime_acceptance_request@1",
            "enabled":false
          }
        }'::jsonb,
    ALTER COLUMN tenant_policy SET NOT NULL;
ALTER TABLE codegen_connections
    DROP CONSTRAINT IF EXISTS codegen_connections_tenant_policy_check;
ALTER TABLE codegen_connections
    ADD CONSTRAINT codegen_connections_tenant_policy_check CHECK (
        jsonb_typeof(tenant_policy) = 'object'
        AND tenant_policy->>'schema_version'
            = 'tenant_codegen_connection_policy@1'
    );

-- There is no compatibility alias: after migration every caller uses the
-- versioned tenant_policy contract and the unrestricted legacy field is gone.
ALTER TABLE codegen_connections DROP COLUMN IF EXISTS policy;

-- Record the tenant snapshot and the effective operator+tenant policy digest
-- used for each generation/repair. Existing rows remain nullable legacy data.
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS tenant_policy_snapshot JSONB;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS effective_safety_policy_sha256 TEXT;
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_tenant_policy_snapshot_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_tenant_policy_snapshot_check CHECK (
        tenant_policy_snapshot IS NULL OR (
            jsonb_typeof(tenant_policy_snapshot) = 'object'
            AND tenant_policy_snapshot->>'schema_version'
                = 'tenant_codegen_connection_policy@1'
        )
    );
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_effective_policy_sha256_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_effective_policy_sha256_check CHECK (
        effective_safety_policy_sha256 IS NULL
        OR effective_safety_policy_sha256 ~ '^[0-9a-f]{64}$'
    );
