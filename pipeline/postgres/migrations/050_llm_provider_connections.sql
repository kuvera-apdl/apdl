-- Non-secret project provider connections and normalized model inventories.

CREATE TABLE llm_project_provider_connections (
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL
        CHECK (provider IN ('openai', 'anthropic', 'google', 'xai')),
    version BIGINT NOT NULL CHECK (version > 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
    credential_id UUID NOT NULL,
    catalog_version TEXT NOT NULL
        CHECK (catalog_version ~ '^llm-provider-catalog@[1-9][0-9]*$'),
    validated_at TIMESTAMPTZ NOT NULL,
    validated_by_actor TEXT NOT NULL
        CHECK (
            validated_by_actor = BTRIM(validated_by_actor)
            AND LENGTH(validated_by_actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN validated_by_actor) = 0
            AND POSITION(CHR(13) IN validated_by_actor) = 0
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    CONSTRAINT llm_project_provider_connections_pkey
        PRIMARY KEY (project_id, provider),
    CONSTRAINT llm_project_provider_connections_version_key
        UNIQUE (project_id, provider, version),
    CONSTRAINT llm_project_provider_connections_credential_fk
        FOREIGN KEY (credential_id, project_id, provider)
        REFERENCES llm_project_provider_credentials (
            credential_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_project_provider_connections_lifecycle_check CHECK (
        (state = 'active' AND revoked_at IS NULL)
        OR (state = 'revoked' AND revoked_at IS NOT NULL)
    )
);

CREATE INDEX llm_project_provider_connections_project_idx
    ON llm_project_provider_connections (project_id, updated_at DESC);

CREATE TABLE llm_project_provider_models (
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    connection_version BIGINT NOT NULL,
    schema_version TEXT NOT NULL
        CHECK (schema_version = 'llm_provider_model@1'),
    model_id TEXT NOT NULL
        CHECK (
            LENGTH(model_id) BETWEEN 1 AND 128
            AND model_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'
        ),
    display_name TEXT NOT NULL
        CHECK (
            display_name = BTRIM(display_name)
            AND LENGTH(display_name) BETWEEN 1 AND 200
        ),
    supported_tiers TEXT[] NOT NULL
        CHECK (
            supported_tiers IN (
                ARRAY['fast']::TEXT[],
                ARRAY['reasoning']::TEXT[],
                ARRAY['fast', 'reasoning']::TEXT[]
            )
        ),
    catalog_version TEXT NOT NULL
        CHECK (catalog_version ~ '^llm-provider-catalog@[1-9][0-9]*$'),
    data_residency TEXT NOT NULL
        CHECK (data_residency IN ('ca', 'us', 'eu', 'global')),
    allowed_data_classifications TEXT[] NOT NULL
        CHECK (
            allowed_data_classifications IN (
                ARRAY['public']::TEXT[],
                ARRAY['public', 'internal']::TEXT[],
                ARRAY['public', 'internal', 'confidential']::TEXT[],
                ARRAY[
                    'public', 'internal', 'confidential', 'restricted'
                ]::TEXT[]
            )
        ),
    pricing_status TEXT NOT NULL
        CHECK (pricing_status = 'operator_review_required'),
    discovered_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT llm_project_provider_models_pkey
        PRIMARY KEY (project_id, provider, model_id),
    CONSTRAINT llm_project_provider_models_connection_fk
        FOREIGN KEY (project_id, provider, connection_version)
        REFERENCES llm_project_provider_connections (
            project_id, provider, version
        ) ON DELETE CASCADE
);

CREATE INDEX llm_project_provider_models_tier_idx
    ON llm_project_provider_models (
        project_id, provider, connection_version, model_id
    );

CREATE TABLE llm_project_provider_connection_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL
        CHECK (provider IN ('openai', 'anthropic', 'google', 'xai')),
    action TEXT NOT NULL
        CHECK (action IN ('connect', 'replace', 'refresh', 'revoke')),
    outcome TEXT NOT NULL CHECK (outcome = 'succeeded'),
    connection_version BIGINT NOT NULL CHECK (connection_version > 0),
    credential_id UUID NOT NULL,
    actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    model_count INTEGER NOT NULL CHECK (model_count BETWEEN 0 AND 1000),
    catalog_version TEXT NOT NULL
        CHECK (catalog_version ~ '^llm-provider-catalog@[1-9][0-9]*$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_project_provider_connection_audit_credential_fk
        FOREIGN KEY (credential_id, project_id, provider)
        REFERENCES llm_project_provider_credentials (
            credential_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_project_provider_connection_audit_shape_check CHECK (
        (action = 'revoke' AND model_count = 0)
        OR (action <> 'revoke' AND model_count > 0)
    )
);

CREATE INDEX llm_project_provider_connection_audit_project_created_idx
    ON llm_project_provider_connection_audit (
        project_id, provider, created_at DESC, audit_id DESC
    );

CREATE OR REPLACE FUNCTION apdl_reject_llm_connection_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_llm_connection_audit_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'LLM provider connection audit rows are immutable';
END
$apdl_reject_llm_connection_audit_mutation$;

CREATE TRIGGER llm_project_provider_connection_audit_no_update_delete
BEFORE UPDATE OR DELETE ON llm_project_provider_connection_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_llm_connection_audit_mutation();

CREATE TRIGGER llm_project_provider_connection_audit_no_truncate
BEFORE TRUNCATE ON llm_project_provider_connection_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_llm_connection_audit_mutation();
