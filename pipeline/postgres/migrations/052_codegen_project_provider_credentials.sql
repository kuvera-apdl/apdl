-- Codegen-owned project-scoped remote LLM credentials.
--
-- PostgreSQL stores ciphertext and non-secret lifecycle metadata only. The
-- AES-256-GCM key remains a Codegen controller deployment secret.

CREATE TABLE codegen_project_provider_credentials (
    credential_id UUID PRIMARY KEY,
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL
        CHECK (provider IN ('anthropic', 'openai', 'google', 'xai')),
    credential_version BIGINT NOT NULL CHECK (credential_version > 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'replaced', 'revoked')),
    ciphertext BYTEA,
    nonce BYTEA,
    algorithm TEXT NOT NULL CHECK (algorithm = 'AES-256-GCM'),
    schema_version TEXT NOT NULL
        CHECK (schema_version = 'codegen_llm_provider_credential@1'),
    encryption_key_id TEXT NOT NULL
        CHECK (encryption_key_id ~ '^sha256:[0-9a-f]{32}$'),
    successor_credential_id UUID,
    created_by_actor TEXT NOT NULL
        CHECK (
            created_by_actor = BTRIM(created_by_actor)
            AND LENGTH(created_by_actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN created_by_actor) = 0
            AND POSITION(CHR(13) IN created_by_actor) = 0
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_by_actor TEXT,
    retirement_reason TEXT,
    retired_at TIMESTAMPTZ,
    CONSTRAINT codegen_project_provider_credentials_version_key
        UNIQUE (project_id, provider, credential_version),
    CONSTRAINT codegen_project_provider_credentials_identity_key
        UNIQUE (credential_id, project_id, provider),
    CONSTRAINT codegen_project_provider_credentials_attempt_identity_key
        UNIQUE (credential_id, project_id, provider, credential_version),
    CONSTRAINT codegen_project_provider_credentials_successor_not_self_check
        CHECK (
            successor_credential_id IS NULL
            OR successor_credential_id <> credential_id
        ),
    CONSTRAINT codegen_project_provider_credentials_retired_actor_check CHECK (
        retired_by_actor IS NULL
        OR (
            retired_by_actor = BTRIM(retired_by_actor)
            AND LENGTH(retired_by_actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN retired_by_actor) = 0
            AND POSITION(CHR(13) IN retired_by_actor) = 0
        )
    ),
    CONSTRAINT codegen_project_provider_credentials_reason_check CHECK (
        retirement_reason IS NULL
        OR retirement_reason IN (
            'provider_connection_replaced',
            'provider_connection_revoked'
        )
    ),
    CONSTRAINT codegen_project_provider_credentials_lifecycle_check CHECK (
        (
            state = 'active'
            AND ciphertext IS NOT NULL
            AND OCTET_LENGTH(ciphertext) > 16
            AND nonce IS NOT NULL
            AND OCTET_LENGTH(nonce) = 12
            AND successor_credential_id IS NULL
            AND retired_by_actor IS NULL
            AND retirement_reason IS NULL
            AND retired_at IS NULL
        )
        OR (
            state = 'replaced'
            AND ciphertext IS NULL
            AND nonce IS NULL
            AND successor_credential_id IS NOT NULL
            AND retired_by_actor IS NOT NULL
            AND retirement_reason = 'provider_connection_replaced'
            AND retired_at IS NOT NULL
        )
        OR (
            state = 'revoked'
            AND ciphertext IS NULL
            AND nonce IS NULL
            AND successor_credential_id IS NULL
            AND retired_by_actor IS NOT NULL
            AND retirement_reason = 'provider_connection_revoked'
            AND retired_at IS NOT NULL
        )
    )
);

ALTER TABLE codegen_project_provider_credentials
    ADD CONSTRAINT codegen_project_provider_credentials_successor_fk
    FOREIGN KEY (successor_credential_id, project_id, provider)
    REFERENCES codegen_project_provider_credentials (
        credential_id, project_id, provider
    )
    DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX codegen_project_provider_credentials_one_active_idx
    ON codegen_project_provider_credentials (project_id, provider)
    WHERE state = 'active';
CREATE UNIQUE INDEX codegen_project_provider_credentials_nonce_idx
    ON codegen_project_provider_credentials (encryption_key_id, nonce)
    WHERE nonce IS NOT NULL;
CREATE INDEX codegen_project_provider_credentials_project_created_idx
    ON codegen_project_provider_credentials (
        project_id, provider, created_at DESC, credential_id DESC
    );

CREATE TABLE codegen_project_provider_credential_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL
        CHECK (provider IN ('anthropic', 'openai', 'google', 'xai')),
    credential_id UUID NOT NULL,
    predecessor_credential_id UUID,
    action TEXT NOT NULL
        CHECK (action IN ('create', 'replace', 'revoke', 'reencrypt')),
    outcome TEXT NOT NULL CHECK (outcome = 'succeeded'),
    actor TEXT NOT NULL
        CHECK (
            actor = BTRIM(actor)
            AND LENGTH(actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN actor) = 0
            AND POSITION(CHR(13) IN actor) = 0
        ),
    encryption_key_id TEXT NOT NULL
        CHECK (encryption_key_id ~ '^sha256:[0-9a-f]{32}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT codegen_project_provider_credential_audit_credential_fk
        FOREIGN KEY (credential_id, project_id, provider)
        REFERENCES codegen_project_provider_credentials (
            credential_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT codegen_project_provider_credential_audit_predecessor_fk
        FOREIGN KEY (predecessor_credential_id, project_id, provider)
        REFERENCES codegen_project_provider_credentials (
            credential_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT codegen_project_provider_credential_audit_shape_check CHECK (
        (action = 'create' AND predecessor_credential_id IS NULL)
        OR (
            action = 'replace'
            AND predecessor_credential_id IS NOT NULL
            AND predecessor_credential_id <> credential_id
        )
        OR (
            action IN ('revoke', 'reencrypt')
            AND predecessor_credential_id IS NULL
        )
    )
);

CREATE INDEX codegen_project_provider_credential_audit_project_created_idx
    ON codegen_project_provider_credential_audit (
        project_id, provider, created_at DESC, audit_id DESC
    );

CREATE OR REPLACE FUNCTION apdl_reject_codegen_credential_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'Codegen provider credential audit rows are immutable';
END
$$;

CREATE TRIGGER codegen_project_provider_credential_audit_no_update_delete
BEFORE UPDATE OR DELETE ON codegen_project_provider_credential_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_codegen_credential_audit_mutation();

CREATE TRIGGER codegen_project_provider_credential_audit_no_truncate
BEFORE TRUNCATE ON codegen_project_provider_credential_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_codegen_credential_audit_mutation();
