-- Migration 055: Replace operator-evaluated publication with exact tenant
-- assignment authority. Apply with Codegen workers stopped. Historical
-- publication_authorization@4 and snapshot@1 artifacts remain audit-only; they
-- cannot be upgraded truthfully into the new active contracts.

ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS publication_authorization_pre_tenant_legacy JSONB,
    ADD COLUMN IF NOT EXISTS llm_execution_snapshot_v1_legacy JSONB;

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_publication_authorization_check;

UPDATE codegen_changesets
SET publication_authorization_pre_tenant_legacy = COALESCE(
        publication_authorization_pre_tenant_legacy,
        publication_authorization
    ),
    publication_authorization = NULL
WHERE publication_authorization IS NOT NULL;

DROP TRIGGER IF EXISTS codegen_changesets_protect_llm_snapshot
    ON codegen_changesets;
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_llm_execution_snapshot_check;

UPDATE codegen_changesets
SET llm_execution_snapshot_v1_legacy = COALESCE(
        llm_execution_snapshot_v1_legacy,
        llm_execution_snapshot
    ),
    llm_execution_snapshot = NULL,
    status = CASE
        WHEN status IN ('queued', 'cloning', 'editing', 'pushing', 'pr_open')
            THEN 'error'
        ELSE status
    END,
    error = CASE
        WHEN status IN ('queued', 'cloning', 'editing', 'pushing', 'pr_open')
            THEN COALESCE(
                error,
                'Legacy Codegen assignment snapshot requires an explicit retry'
            )
        ELSE error
    END,
    updated_at = NOW()
WHERE llm_execution_snapshot->>'schema_version'
    = 'codegen_llm_execution_snapshot@1';

CREATE OR REPLACE FUNCTION apdl_is_codegen_llm_execution_snapshot(
    snapshot JSONB,
    expected_project_id TEXT,
    expected_grant_id TEXT,
    expected_repository_id BIGINT,
    expected_installation_id BIGINT,
    expected_repository_full_name TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(snapshot) = 'object'
        AND snapshot ?& ARRAY[
            'schema_version', 'project_id', 'repository_grant_id',
            'repository_id', 'repository_installation_id',
            'repository_full_name', 'codegen_revision',
            'behavior_configuration_sha256', 'rollout_stage', 'assignments'
        ]::TEXT[]
        AND snapshot - ARRAY[
            'schema_version', 'project_id', 'repository_grant_id',
            'repository_id', 'repository_installation_id',
            'repository_full_name', 'codegen_revision',
            'behavior_configuration_sha256', 'rollout_stage', 'assignments'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(snapshot->'schema_version') = 'string'
        AND snapshot->>'schema_version'
            = 'codegen_llm_execution_snapshot@2'
        AND jsonb_typeof(snapshot->'project_id') = 'string'
        AND snapshot->>'project_id' = expected_project_id
        AND jsonb_typeof(snapshot->'repository_grant_id') = 'string'
        AND LENGTH(snapshot->>'repository_grant_id') BETWEEN 5 AND 132
        AND snapshot->>'repository_grant_id' ~ '^ghg_[A-Za-z0-9_-]+$'
        AND snapshot->>'repository_grant_id' = expected_grant_id
        AND jsonb_typeof(snapshot->'repository_id') = 'number'
        AND snapshot->>'repository_id' ~ '^[1-9][0-9]*$'
        AND (snapshot->>'repository_id')::BIGINT = expected_repository_id
        AND jsonb_typeof(snapshot->'repository_installation_id') = 'number'
        AND snapshot->>'repository_installation_id' ~ '^[1-9][0-9]*$'
        AND (snapshot->>'repository_installation_id')::BIGINT
            = expected_installation_id
        AND jsonb_typeof(snapshot->'repository_full_name') = 'string'
        AND LENGTH(snapshot->>'repository_full_name') BETWEEN 3 AND 201
        AND snapshot->>'repository_full_name' = expected_repository_full_name
        AND jsonb_typeof(snapshot->'codegen_revision') = 'string'
        AND LENGTH(snapshot->>'codegen_revision') BETWEEN 1 AND 200
        AND snapshot->>'codegen_revision'
            = BTRIM(snapshot->>'codegen_revision')
        AND jsonb_typeof(snapshot->'behavior_configuration_sha256') = 'string'
        AND snapshot->>'behavior_configuration_sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(snapshot->'rollout_stage') = 'string'
        AND snapshot->>'rollout_stage' IN (
            'offline', 'development_pr', 'tenant_draft_pr'
        )
        AND jsonb_typeof(snapshot->'assignments') = 'array'
        AND jsonb_array_length(snapshot->'assignments') = 2
        AND apdl_is_codegen_llm_assignment_snapshot(
            snapshot->'assignments'->0, 'editor'
        )
        AND apdl_is_codegen_llm_assignment_snapshot(
            snapshot->'assignments'->1, 'helper'
        )
    ) IS TRUE
$$;

ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_llm_execution_snapshot_check CHECK (
        llm_execution_snapshot IS NULL
        OR (
            apdl_is_codegen_llm_execution_snapshot(
                llm_execution_snapshot,
                project_id,
                repository_grant_id,
                repository_id,
                repository_installation_id,
                repository_full_name
            )
        ) IS TRUE
    );

CREATE TRIGGER codegen_changesets_protect_llm_snapshot
BEFORE INSERT OR UPDATE OF llm_execution_snapshot ON codegen_changesets
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_codegen_llm_snapshot();

CREATE OR REPLACE FUNCTION apdl_is_tenant_publication_runtime_identity(
    identity JSONB
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(identity) = 'object'
        AND identity ?& ARRAY[
            'schema_version', 'controller_image_id', 'worker_image_id',
            'codegen_revision', 'behavior_configuration_sha256',
            'egress_policy_sha256', 'egress_proxy_image_id',
            'egress_transport', 'max_concurrent_jobs', 'identity_sha256'
        ]::TEXT[]
        AND identity - ARRAY[
            'schema_version', 'controller_image_id', 'worker_image_id',
            'codegen_revision', 'behavior_configuration_sha256',
            'egress_policy_sha256', 'egress_proxy_image_id',
            'egress_transport', 'max_concurrent_jobs', 'identity_sha256'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(identity->'schema_version') = 'string'
        AND identity->>'schema_version'
            = 'tenant_publication_runtime_identity@1'
        AND jsonb_typeof(identity->'controller_image_id') = 'string'
        AND identity->>'controller_image_id' ~ '^sha256:[0-9a-f]{64}$'
        AND jsonb_typeof(identity->'worker_image_id') = 'string'
        AND identity->>'worker_image_id' ~ '^sha256:[0-9a-f]{64}$'
        AND jsonb_typeof(identity->'codegen_revision') = 'string'
        AND LENGTH(identity->>'codegen_revision') BETWEEN 1 AND 200
        AND identity->>'codegen_revision'
            = BTRIM(identity->>'codegen_revision')
        AND jsonb_typeof(identity->'behavior_configuration_sha256') = 'string'
        AND identity->>'behavior_configuration_sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(identity->'egress_policy_sha256') = 'string'
        AND identity->>'egress_policy_sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(identity->'egress_proxy_image_id') = 'string'
        AND identity->>'egress_proxy_image_id' ~ '^sha256:[0-9a-f]{64}$'
        AND jsonb_typeof(identity->'egress_transport') = 'string'
        AND identity->>'egress_transport' = 'network_none_unix_socket@1'
        AND identity->'max_concurrent_jobs' = '1'::JSONB
        AND jsonb_typeof(identity->'identity_sha256') = 'string'
        AND identity->>'identity_sha256' ~ '^[0-9a-f]{64}$'
    ) IS TRUE
$$;

CREATE OR REPLACE FUNCTION apdl_is_tenant_publication_request(
    request JSONB,
    expected_snapshot JSONB,
    expected_risk TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(request) = 'object'
        AND request ?& ARRAY[
            'schema_version', 'requested_stage', 'risk',
            'execution_snapshot', 'execution_snapshot_sha256',
            'runtime_identity'
        ]::TEXT[]
        AND request - ARRAY[
            'schema_version', 'requested_stage', 'risk',
            'execution_snapshot', 'execution_snapshot_sha256',
            'runtime_identity'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(request->'schema_version') = 'string'
        AND request->>'schema_version' = 'tenant_publication_request@1'
        AND jsonb_typeof(request->'requested_stage') = 'string'
        AND request->>'requested_stage' = 'tenant_draft_pr'
        AND jsonb_typeof(request->'risk') = 'string'
        AND request->>'risk' = expected_risk
        AND request->'execution_snapshot' = expected_snapshot
        AND request->'execution_snapshot'->>'rollout_stage' = 'tenant_draft_pr'
        AND jsonb_typeof(request->'execution_snapshot_sha256') = 'string'
        AND request->>'execution_snapshot_sha256' ~ '^[0-9a-f]{64}$'
        AND apdl_is_tenant_publication_runtime_identity(
            request->'runtime_identity'
        )
        AND request->'runtime_identity'->>'codegen_revision'
            = expected_snapshot->>'codegen_revision'
        AND request->'runtime_identity'->>'behavior_configuration_sha256'
            = expected_snapshot->>'behavior_configuration_sha256'
    ) IS TRUE
$$;

CREATE OR REPLACE FUNCTION apdl_is_tenant_publication_decision(
    decision JSONB,
    expected_risk TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(decision) = 'object'
        AND decision ?& ARRAY[
            'schema_version', 'requested_stage', 'risk', 'allowed',
            'publish_branch', 'create_pull_request', 'ready_for_review',
            'reasons', 'decision_sha256'
        ]::TEXT[]
        AND decision - ARRAY[
            'schema_version', 'requested_stage', 'risk', 'allowed',
            'publish_branch', 'create_pull_request', 'ready_for_review',
            'reasons', 'decision_sha256'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(decision->'schema_version') = 'string'
        AND decision->>'schema_version' = 'tenant_publication_decision@1'
        AND jsonb_typeof(decision->'requested_stage') = 'string'
        AND decision->>'requested_stage' = 'tenant_draft_pr'
        AND jsonb_typeof(decision->'risk') = 'string'
        AND decision->>'risk' = expected_risk
        AND decision->'allowed' = 'true'::JSONB
        AND decision->'publish_branch' = 'true'::JSONB
        AND decision->'create_pull_request' = 'true'::JSONB
        AND decision->'ready_for_review' = 'false'::JSONB
        AND decision->'reasons' = '[]'::JSONB
        AND jsonb_typeof(decision->'decision_sha256') = 'string'
        AND decision->>'decision_sha256' ~ '^[0-9a-f]{64}$'
    ) IS TRUE
$$;

CREATE OR REPLACE FUNCTION apdl_is_development_publication_authorization(
    document JSONB,
    expected_snapshot JSONB,
    expected_risk TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(document) = 'object'
        AND document ?& ARRAY[
            'schema_version', 'authority', 'request', 'decision',
            'draft_only', 'authorization_sha256'
        ]::TEXT[]
        AND document - ARRAY[
            'schema_version', 'authority', 'request', 'decision',
            'draft_only', 'authorization_sha256'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(document->'schema_version') = 'string'
        AND document->>'schema_version'
            = 'development_publication_authorization@1'
        AND jsonb_typeof(document->'authority') = 'string'
        AND document->>'authority' = 'local_development'
        AND document->'draft_only' = 'true'::JSONB
        AND jsonb_typeof(document->'authorization_sha256') = 'string'
        AND document->>'authorization_sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(expected_snapshot) = 'object'
        AND expected_snapshot->>'schema_version'
            = 'codegen_llm_execution_snapshot@2'
        AND expected_snapshot->>'rollout_stage' = 'development_pr'
        AND expected_snapshot->>'codegen_revision' = 'local-development'
        AND jsonb_typeof(document->'request') = 'object'
        AND document->'request' ?& ARRAY[
            'schema_version', 'requested_stage', 'risk', 'model',
            'codegen_revision'
        ]::TEXT[]
        AND (document->'request') - ARRAY[
            'schema_version', 'requested_stage', 'risk', 'model',
            'codegen_revision'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(document->'request'->'schema_version') = 'string'
        AND document->'request'->>'schema_version'
            = 'development_publication_request@1'
        AND jsonb_typeof(document->'request'->'requested_stage') = 'string'
        AND document->'request'->>'requested_stage' = 'development_pr'
        AND jsonb_typeof(document->'request'->'risk') = 'string'
        AND document->'request'->>'risk' = expected_risk
        AND jsonb_typeof(document->'request'->'codegen_revision') = 'string'
        AND document->'request'->>'codegen_revision' = 'local-development'
        AND document->'request'->>'codegen_revision'
            = expected_snapshot->>'codegen_revision'
        AND jsonb_typeof(document->'request'->'model') = 'string'
        AND LENGTH(document->'request'->>'model') > 0
        AND document->'request'->>'model' = (
            CASE (expected_snapshot->'assignments'->0->>'provider')
                WHEN 'anthropic' THEN 'anthropic/'
                WHEN 'openai' THEN 'openai/'
                WHEN 'google' THEN 'gemini/'
                WHEN 'xai' THEN 'xai/'
                ELSE NULL
            END
            || (expected_snapshot->'assignments'->0->>'model_id')
        )
        AND jsonb_typeof(document->'decision') = 'object'
        AND document->'decision' ?& ARRAY[
            'schema_version', 'requested_stage', 'risk', 'allowed',
            'publish_branch', 'create_pull_request', 'ready_for_review',
            'reasons', 'decision_sha256'
        ]::TEXT[]
        AND (document->'decision') - ARRAY[
            'schema_version', 'requested_stage', 'risk', 'allowed',
            'publish_branch', 'create_pull_request', 'ready_for_review',
            'reasons', 'decision_sha256'
        ]::TEXT[] = '{}'::JSONB
        AND jsonb_typeof(document->'decision'->'schema_version') = 'string'
        AND document->'decision'->>'schema_version'
            = 'development_publication_decision@1'
        AND jsonb_typeof(document->'decision'->'requested_stage') = 'string'
        AND document->'decision'->>'requested_stage' = 'development_pr'
        AND jsonb_typeof(document->'decision'->'risk') = 'string'
        AND document->'decision'->>'risk' = expected_risk
        AND document->'decision'->'allowed' = 'true'::JSONB
        AND document->'decision'->'publish_branch' = 'true'::JSONB
        AND document->'decision'->'create_pull_request' = 'true'::JSONB
        AND document->'decision'->'ready_for_review' = 'false'::JSONB
        AND document->'decision'->'reasons' = '[]'::JSONB
        AND jsonb_typeof(document->'decision'->'decision_sha256') = 'string'
        AND document->'decision'->>'decision_sha256' ~ '^[0-9a-f]{64}$'
    ) IS TRUE
$$;

ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_publication_authorization_check CHECK ((
        publication_authorization IS NULL
        OR (
            publication_authorization->>'schema_version'
                = 'tenant_publication_authorization@1'
            AND publication_authorization ?& ARRAY[
                'schema_version', 'authority', 'request', 'decision',
                'draft_only', 'authorization_sha256'
            ]::TEXT[]
            AND publication_authorization - ARRAY[
                'schema_version', 'authority', 'request', 'decision',
                'draft_only', 'authorization_sha256'
            ]::TEXT[] = '{}'::JSONB
            AND jsonb_typeof(
                publication_authorization->'schema_version'
            ) = 'string'
            AND publication_authorization->>'authority'
                = 'tenant_model_assignments'
            AND jsonb_typeof(publication_authorization->'authority') = 'string'
            AND publication_authorization->'draft_only' = 'true'::JSONB
            AND jsonb_typeof(
                publication_authorization->'authorization_sha256'
            ) = 'string'
            AND publication_authorization->>'authorization_sha256'
                ~ '^[0-9a-f]{64}$'
            AND apdl_is_tenant_publication_request(
                publication_authorization->'request',
                llm_execution_snapshot,
                control_metadata->>'risk_level'
            )
            AND apdl_is_tenant_publication_decision(
                publication_authorization->'decision',
                control_metadata->>'risk_level'
            )
        )
        OR apdl_is_development_publication_authorization(
            publication_authorization,
            llm_execution_snapshot,
            control_metadata->>'risk_level'
        )
    ) IS TRUE);

COMMENT ON COLUMN
    codegen_changesets.publication_authorization_pre_tenant_legacy IS
    'Audit-only publication authority retired before tenant-scoped publication.';
COMMENT ON COLUMN codegen_changesets.llm_execution_snapshot_v1_legacy IS
    'Audit-only codegen_llm_execution_snapshot@1 retired before tenant publication.';
COMMENT ON COLUMN codegen_changesets.publication_authorization IS
    'Strict tenant_publication_authorization@1 or local development_publication_authorization@1.';
