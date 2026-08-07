-- Canonical project LLM credential custody shared by Agents and Codegen.
--
-- The legacy services encrypted different tables with different deployment
-- keys. Ciphertext and credential history cannot be moved safely in SQL, so
-- this fresh-install-only migration refuses to run when either legacy store
-- contains any row. Revocation crypto-shreds secret material but deliberately
-- retains lifecycle history, so it cannot make an existing database eligible
-- for this cutover. The new vault keeps secret bytes in a table that
-- apdl_runtime cannot read; consumers see only non-secret version and lifecycle
-- metadata.

DO $apdl_require_empty_legacy_llm_credential_stores$
BEGIN
    IF EXISTS (SELECT 1 FROM llm_project_provider_credentials)
       OR EXISTS (SELECT 1 FROM codegen_project_provider_credentials) THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'project LLM vault migration does not support legacy credential history',
            HINT = 'Initialize a fresh PostgreSQL database, apply the canonical migrations, and reconnect provider credentials; revocation does not remove legacy credential history.';
    END IF;
END
$apdl_require_empty_legacy_llm_credential_stores$;

DO $apdl_ensure_llm_vault_role$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'apdl_llm_vault'
    ) THEN
        CREATE ROLE apdl_llm_vault NOLOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
            NOREPLICATION NOBYPASSRLS;
    END IF;
END
$apdl_ensure_llm_vault_role$;

REVOKE CREATE ON SCHEMA public FROM apdl_llm_vault;
GRANT USAGE ON SCHEMA public TO apdl_llm_vault;

DO $apdl_grant_llm_vault_connect$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO apdl_llm_vault',
        current_database()
    );
END
$apdl_grant_llm_vault_connect$;

CREATE TABLE llm_vault_connections (
    connection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    provider TEXT NOT NULL
        CHECK (provider IN ('anthropic', 'openai', 'google', 'xai')),
    label TEXT NOT NULL CHECK (
        label = BTRIM(label)
        AND LENGTH(label) BETWEEN 1 AND 80
        AND POSITION(CHR(10) IN label) = 0
        AND POSITION(CHR(13) IN label) = 0
    ),
    version BIGINT NOT NULL CHECK (version > 0),
    inventory_version BIGINT NOT NULL CHECK (inventory_version > 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
    validated_at TIMESTAMPTZ NOT NULL,
    created_by_actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_by_actor_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    revocation_reason TEXT,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT llm_vault_connections_project_label_key
        UNIQUE (project_id, provider, label),
    CONSTRAINT llm_vault_connections_identity_key
        UNIQUE (connection_id, project_id, provider),
    CONSTRAINT llm_vault_connections_version_key
        UNIQUE (connection_id, version),
    CONSTRAINT llm_vault_connections_lifecycle_check CHECK (
        (
            state = 'active'
            AND revoked_by_actor_user_id IS NULL
            AND revocation_reason IS NULL
            AND revoked_at IS NULL
        )
        OR (
            state = 'revoked'
            AND revoked_by_actor_user_id IS NOT NULL
            AND revocation_reason IS NOT NULL
            AND revocation_reason = BTRIM(revocation_reason)
            AND LENGTH(revocation_reason) BETWEEN 1 AND 2000
            AND POSITION(CHR(10) IN revocation_reason) = 0
            AND POSITION(CHR(13) IN revocation_reason) = 0
            AND revoked_at IS NOT NULL
        )
    )
);

CREATE INDEX llm_vault_connections_project_idx
    ON llm_vault_connections (project_id, provider, updated_at DESC);

CREATE TABLE llm_vault_provider_credentials (
    credential_id UUID PRIMARY KEY,
    connection_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    credential_version BIGINT NOT NULL CHECK (credential_version > 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'replaced', 'revoked')),
    successor_credential_id UUID,
    created_by_actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_by_actor_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    retirement_reason TEXT,
    retired_at TIMESTAMPTZ,
    CONSTRAINT llm_vault_provider_credentials_connection_fk
        FOREIGN KEY (connection_id, project_id, provider)
        REFERENCES llm_vault_connections (
            connection_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_vault_provider_credentials_version_key
        UNIQUE (connection_id, credential_version),
    CONSTRAINT llm_vault_provider_credentials_identity_key
        UNIQUE (credential_id, project_id, provider),
    CONSTRAINT llm_vault_provider_credentials_attempt_identity_key
        UNIQUE (credential_id, project_id, provider, credential_version),
    CONSTRAINT llm_vault_provider_credentials_lifecycle_check CHECK (
        (
            state = 'active'
            AND successor_credential_id IS NULL
            AND retired_by_actor_user_id IS NULL
            AND retirement_reason IS NULL
            AND retired_at IS NULL
        )
        OR (
            state = 'replaced'
            AND successor_credential_id IS NOT NULL
            AND successor_credential_id <> credential_id
            AND retired_by_actor_user_id IS NOT NULL
            AND retirement_reason IS NOT NULL
            AND retired_at IS NOT NULL
        )
        OR (
            state = 'revoked'
            AND successor_credential_id IS NULL
            AND retired_by_actor_user_id IS NOT NULL
            AND retirement_reason IS NOT NULL
            AND retired_at IS NOT NULL
        )
    ),
    CONSTRAINT llm_vault_provider_credentials_reason_check CHECK (
        retirement_reason IS NULL
        OR (
            retirement_reason = BTRIM(retirement_reason)
            AND LENGTH(retirement_reason) BETWEEN 1 AND 2000
            AND POSITION(CHR(10) IN retirement_reason) = 0
            AND POSITION(CHR(13) IN retirement_reason) = 0
        )
    )
);

ALTER TABLE llm_vault_provider_credentials
    ADD CONSTRAINT llm_vault_provider_credentials_successor_fk
    FOREIGN KEY (successor_credential_id, project_id, provider)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider
    ) DEFERRABLE INITIALLY DEFERRED;

CREATE UNIQUE INDEX llm_vault_provider_credentials_one_active_idx
    ON llm_vault_provider_credentials (connection_id)
    WHERE state = 'active';
CREATE INDEX llm_vault_provider_credentials_project_idx
    ON llm_vault_provider_credentials (
        project_id, provider, created_at DESC, credential_id DESC
    );

CREATE TABLE llm_vault_provider_secrets (
    credential_id UUID PRIMARY KEY
        REFERENCES llm_vault_provider_credentials(credential_id)
        ON DELETE RESTRICT,
    ciphertext BYTEA NOT NULL CHECK (OCTET_LENGTH(ciphertext) > 16),
    nonce BYTEA NOT NULL CHECK (OCTET_LENGTH(nonce) = 12),
    algorithm TEXT NOT NULL CHECK (algorithm = 'AES-256-GCM'),
    schema_version TEXT NOT NULL
        CHECK (schema_version = 'llm_vault_provider_secret@1'),
    encryption_key_id TEXT NOT NULL
        CHECK (encryption_key_id ~ '^sha256:[0-9a-f]{32}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_vault_provider_secrets_nonce_key
        UNIQUE (encryption_key_id, nonce)
);

CREATE TABLE llm_vault_connection_consumers (
    connection_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    consumer TEXT NOT NULL CHECK (consumer IN ('agents', 'codegen')),
    granted_by_actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (connection_id, consumer),
    CONSTRAINT llm_vault_connection_consumers_connection_fk
        FOREIGN KEY (connection_id, project_id, provider)
        REFERENCES llm_vault_connections (
            connection_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_vault_connection_consumers_one_binding_key
        UNIQUE (project_id, provider, consumer)
);

CREATE TABLE llm_vault_provider_models (
    connection_id UUID NOT NULL,
    connection_version BIGINT NOT NULL,
    inventory_version BIGINT NOT NULL CHECK (inventory_version > 0),
    model_id TEXT NOT NULL CHECK (
        LENGTH(model_id) BETWEEN 1 AND 128
        AND model_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'
    ),
    discovered_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (connection_id, inventory_version, model_id),
    CONSTRAINT llm_vault_provider_models_connection_fk
        FOREIGN KEY (connection_id, connection_version)
        REFERENCES llm_vault_connections (connection_id, version)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE OR REPLACE FUNCTION apdl_validate_llm_vault_connection_authority(
    candidate_connection_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
AS $apdl_validate_llm_vault_connection_authority$
DECLARE
    current_connection RECORD;
    active_credential_count INTEGER;
    secret_count INTEGER;
    consumer_count INTEGER;
    model_count INTEGER;
    current_model_count INTEGER;
BEGIN
    SELECT state, version, inventory_version
    INTO current_connection
    FROM llm_vault_connections
    WHERE connection_id = candidate_connection_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT
        COUNT(*) FILTER (WHERE credential.state = 'active'),
        COUNT(secret.credential_id)
    INTO active_credential_count, secret_count
    FROM llm_vault_provider_credentials AS credential
    LEFT JOIN llm_vault_provider_secrets AS secret
      ON secret.credential_id = credential.credential_id
    WHERE credential.connection_id = candidate_connection_id;

    SELECT COUNT(*)
    INTO consumer_count
    FROM llm_vault_connection_consumers
    WHERE connection_id = candidate_connection_id;

    SELECT
        COUNT(*),
        COUNT(*) FILTER (
            WHERE connection_version = current_connection.version
              AND inventory_version = current_connection.inventory_version
        )
    INTO model_count, current_model_count
    FROM llm_vault_provider_models
    WHERE connection_id = candidate_connection_id;

    IF current_connection.state = 'active' THEN
        IF active_credential_count <> 1
           OR secret_count <> 1
           OR consumer_count NOT BETWEEN 1 AND 2
           OR model_count = 0
           OR current_model_count <> model_count THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'active LLM vault connection requires one secret, explicit consumers, and current models';
        END IF;
    ELSIF active_credential_count <> 0
       OR secret_count <> 0
       OR consumer_count <> 0
       OR model_count <> 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'revoked LLM vault connection cannot retain live authority';
    END IF;
END
$apdl_validate_llm_vault_connection_authority$;

CREATE OR REPLACE FUNCTION apdl_check_llm_vault_connection_authority_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_check_llm_vault_connection_authority_trigger$
DECLARE
    candidate_connection_id UUID;
    candidate_credential_id UUID;
BEGIN
    IF TG_TABLE_NAME = 'llm_vault_provider_secrets' THEN
        candidate_credential_id := CASE
            WHEN TG_OP = 'DELETE' THEN OLD.credential_id
            ELSE NEW.credential_id
        END;
        SELECT connection_id
        INTO candidate_connection_id
        FROM llm_vault_provider_credentials
        WHERE credential_id = candidate_credential_id;
    ELSE
        candidate_connection_id := CASE
            WHEN TG_OP = 'DELETE' THEN OLD.connection_id
            ELSE NEW.connection_id
        END;
    END IF;
    IF candidate_connection_id IS NOT NULL THEN
        PERFORM apdl_validate_llm_vault_connection_authority(
            candidate_connection_id
        );
    END IF;
    RETURN NULL;
END
$apdl_check_llm_vault_connection_authority_trigger$;

CREATE CONSTRAINT TRIGGER llm_vault_connections_validate_authority
AFTER INSERT OR UPDATE ON llm_vault_connections
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_llm_vault_connection_authority_trigger();
CREATE CONSTRAINT TRIGGER llm_vault_credentials_validate_authority
AFTER INSERT OR UPDATE OR DELETE ON llm_vault_provider_credentials
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_llm_vault_connection_authority_trigger();
CREATE CONSTRAINT TRIGGER llm_vault_secrets_validate_authority
AFTER INSERT OR UPDATE OR DELETE ON llm_vault_provider_secrets
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_llm_vault_connection_authority_trigger();
CREATE CONSTRAINT TRIGGER llm_vault_consumers_validate_authority
AFTER INSERT OR UPDATE OR DELETE ON llm_vault_connection_consumers
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_llm_vault_connection_authority_trigger();
CREATE CONSTRAINT TRIGGER llm_vault_models_validate_authority
AFTER INSERT OR UPDATE OR DELETE ON llm_vault_provider_models
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_llm_vault_connection_authority_trigger();

CREATE TABLE llm_vault_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    credential_id UUID NOT NULL,
    credential_version BIGINT NOT NULL CHECK (credential_version > 0),
    action TEXT NOT NULL
        CHECK (action IN ('create', 'replace', 'refresh', 'revoke')),
    outcome TEXT NOT NULL CHECK (outcome = 'succeeded'),
    consumers TEXT[] NOT NULL CHECK (
        consumers IN (
            ARRAY['agents']::TEXT[],
            ARRAY['codegen']::TEXT[],
            ARRAY['agents', 'codegen']::TEXT[]
        )
    ),
    actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_vault_audit_connection_fk
        FOREIGN KEY (connection_id, project_id, provider)
        REFERENCES llm_vault_connections (
            connection_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_vault_audit_credential_fk
        FOREIGN KEY (
            credential_id, project_id, provider, credential_version
        ) REFERENCES llm_vault_provider_credentials (
            credential_id, project_id, provider, credential_version
        ) ON DELETE RESTRICT
);

CREATE INDEX llm_vault_audit_project_idx
    ON llm_vault_audit (project_id, created_at DESC, audit_id DESC);

CREATE TABLE llm_vault_access_audit (
    access_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    credential_id UUID NOT NULL,
    credential_version BIGINT NOT NULL CHECK (credential_version > 0),
    consumer TEXT NOT NULL CHECK (consumer IN ('agents', 'codegen')),
    execution_id TEXT NOT NULL CHECK (
        execution_id = BTRIM(execution_id)
        AND LENGTH(execution_id) BETWEEN 1 AND 256
        AND POSITION(CHR(10) IN execution_id) = 0
        AND POSITION(CHR(13) IN execution_id) = 0
    ),
    purpose TEXT NOT NULL CHECK (
        purpose ~ '^[a-z][a-z0-9_.:-]{0,127}$'
    ),
    outcome TEXT NOT NULL CHECK (outcome = 'issued'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_vault_access_audit_connection_fk
        FOREIGN KEY (connection_id, project_id, provider)
        REFERENCES llm_vault_connections (
            connection_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_vault_access_audit_credential_fk
        FOREIGN KEY (
            credential_id, project_id, provider, credential_version
        ) REFERENCES llm_vault_provider_credentials (
            credential_id, project_id, provider, credential_version
        ) ON DELETE RESTRICT
);

CREATE INDEX llm_vault_access_audit_project_idx
    ON llm_vault_access_audit (
        project_id, consumer, created_at DESC, access_id DESC
    );

CREATE TABLE llm_vault_key_rotation_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    credential_id UUID NOT NULL,
    credential_version BIGINT NOT NULL CHECK (credential_version > 0),
    action TEXT NOT NULL CHECK (action = 'reencrypt'),
    outcome TEXT NOT NULL CHECK (outcome = 'succeeded'),
    operator TEXT NOT NULL CHECK (
        operator = BTRIM(operator)
        AND LENGTH(operator) BETWEEN 1 AND 512
        AND POSITION(CHR(10) IN operator) = 0
        AND POSITION(CHR(13) IN operator) = 0
    ),
    previous_encryption_key_id TEXT NOT NULL
        CHECK (previous_encryption_key_id ~ '^sha256:[0-9a-f]{32}$'),
    encryption_key_id TEXT NOT NULL
        CHECK (encryption_key_id ~ '^sha256:[0-9a-f]{32}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_vault_key_rotation_audit_key_change_check
        CHECK (previous_encryption_key_id <> encryption_key_id),
    CONSTRAINT llm_vault_key_rotation_audit_connection_fk
        FOREIGN KEY (connection_id, project_id, provider)
        REFERENCES llm_vault_connections (
            connection_id, project_id, provider
        ) ON DELETE RESTRICT,
    CONSTRAINT llm_vault_key_rotation_audit_credential_fk
        FOREIGN KEY (
            credential_id, project_id, provider, credential_version
        ) REFERENCES llm_vault_provider_credentials (
            credential_id, project_id, provider, credential_version
        ) ON DELETE RESTRICT
);

CREATE INDEX llm_vault_key_rotation_audit_project_idx
    ON llm_vault_key_rotation_audit (
        project_id, created_at DESC, audit_id DESC
    );

CREATE OR REPLACE FUNCTION apdl_reject_llm_vault_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_llm_vault_audit_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'LLM vault audit rows are immutable';
END
$apdl_reject_llm_vault_audit_mutation$;

CREATE TRIGGER llm_vault_audit_no_update_delete
BEFORE UPDATE OR DELETE ON llm_vault_audit
FOR EACH ROW EXECUTE FUNCTION apdl_reject_llm_vault_audit_mutation();
CREATE TRIGGER llm_vault_audit_no_truncate
BEFORE TRUNCATE ON llm_vault_audit
FOR EACH STATEMENT EXECUTE FUNCTION apdl_reject_llm_vault_audit_mutation();
CREATE TRIGGER llm_vault_access_audit_no_update_delete
BEFORE UPDATE OR DELETE ON llm_vault_access_audit
FOR EACH ROW EXECUTE FUNCTION apdl_reject_llm_vault_audit_mutation();
CREATE TRIGGER llm_vault_access_audit_no_truncate
BEFORE TRUNCATE ON llm_vault_access_audit
FOR EACH STATEMENT EXECUTE FUNCTION apdl_reject_llm_vault_audit_mutation();
CREATE TRIGGER llm_vault_key_rotation_audit_no_update_delete
BEFORE UPDATE OR DELETE ON llm_vault_key_rotation_audit
FOR EACH ROW EXECUTE FUNCTION apdl_reject_llm_vault_audit_mutation();
CREATE TRIGGER llm_vault_key_rotation_audit_no_truncate
BEFORE TRUNCATE ON llm_vault_key_rotation_audit
FOR EACH STATEMENT EXECUTE FUNCTION apdl_reject_llm_vault_audit_mutation();

-- Hold the live project/user/membership authority rows without granting the
-- vault service UPDATE on Admin identity tables. Row-locking SELECT requires
-- UPDATE privilege, so the restricted role calls this fixed security-definer
-- predicate instead of receiving broad write authority.
CREATE OR REPLACE FUNCTION apdl_llm_vault_has_management_authority(
    candidate_project_id TEXT,
    candidate_actor_user_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $apdl_llm_vault_has_management_authority$
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
    FOR KEY SHARE OF project, account;

    IF NOT FOUND OR actor_active IS NOT TRUE THEN
        RETURN FALSE;
    END IF;
    IF project_owner_user_id = candidate_actor_user_id THEN
        RETURN TRUE;
    END IF;

    SELECT membership.roles
    INTO actor_roles
    FROM public.admin_user_projects AS membership
    WHERE membership.project_id = candidate_project_id
      AND membership.user_id = candidate_actor_user_id
    FOR KEY SHARE;

    RETURN COALESCE(
        actor_roles @> ARRAY['agents:manage', 'credentials:manage']::TEXT[],
        FALSE
    );
END
$apdl_llm_vault_has_management_authority$;

REVOKE ALL ON FUNCTION apdl_llm_vault_has_management_authority(TEXT, UUID)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    apdl_llm_vault_has_management_authority(TEXT, UUID)
TO apdl_llm_vault;

-- Redirect the existing consumer projections and immutable attempt history to
-- canonical vault credential metadata before removing legacy secret tables.
ALTER TABLE llm_project_provider_connections
    DROP CONSTRAINT llm_project_provider_connections_credential_fk;
ALTER TABLE llm_project_provider_connection_audit
    DROP CONSTRAINT llm_project_provider_connection_audit_credential_fk;
ALTER TABLE llm_provider_attempts
    DROP CONSTRAINT llm_provider_attempts_credential_fk;
ALTER TABLE codegen_project_provider_connections
    DROP CONSTRAINT codegen_project_provider_connections_credential_fk;
ALTER TABLE codegen_project_provider_connection_audit
    DROP CONSTRAINT codegen_project_provider_connection_audit_credential_fk;
ALTER TABLE codegen_llm_attempts
    DROP CONSTRAINT codegen_llm_attempts_credential_fk;

ALTER TABLE llm_project_provider_connections
    ADD CONSTRAINT llm_project_provider_connections_vault_credential_fk
    FOREIGN KEY (credential_id, project_id, provider)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider
    ) ON DELETE RESTRICT;
ALTER TABLE llm_project_provider_connection_audit
    ADD CONSTRAINT llm_project_provider_connection_audit_vault_credential_fk
    FOREIGN KEY (credential_id, project_id, provider)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider
    ) ON DELETE RESTRICT;
ALTER TABLE llm_provider_attempts
    ADD CONSTRAINT llm_provider_attempts_vault_credential_fk
    FOREIGN KEY (credential_id, project_id, provider, credential_version)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider, credential_version
    ) ON DELETE RESTRICT;
ALTER TABLE codegen_project_provider_connections
    ADD CONSTRAINT codegen_project_provider_connections_vault_credential_fk
    FOREIGN KEY (credential_id, project_id, provider)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider
    ) ON DELETE RESTRICT;
ALTER TABLE codegen_project_provider_connection_audit
    ADD CONSTRAINT codegen_project_provider_connection_audit_vault_credential_fk
    FOREIGN KEY (credential_id, project_id, provider)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider
    ) ON DELETE RESTRICT;
ALTER TABLE codegen_llm_attempts
    ADD CONSTRAINT codegen_llm_attempts_vault_credential_fk
    FOREIGN KEY (credential_id, project_id, provider, credential_version)
    REFERENCES llm_vault_provider_credentials (
        credential_id, project_id, provider, credential_version
    ) ON DELETE RESTRICT;

DROP TRIGGER llm_project_provider_credentials_validate_active_setup
    ON llm_project_provider_credentials;
DROP TABLE llm_project_provider_credential_audit;
DROP TABLE codegen_project_provider_credential_audit;
DROP TABLE llm_project_provider_credentials;
DROP TABLE codegen_project_provider_credentials;

-- The active Agents invariant now includes the exact vault grant as well as
-- current non-secret connection/model authority.
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
    JOIN llm_vault_provider_credentials AS credential
      ON credential.credential_id = connection.credential_id
     AND credential.project_id = connection.project_id
     AND credential.provider = connection.provider
     AND credential.state = 'active'
    JOIN llm_vault_connection_consumers AS consumer
      ON consumer.connection_id = credential.connection_id
     AND consumer.project_id = credential.project_id
     AND consumer.provider = credential.provider
     AND consumer.consumer = 'agents'
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
           SELECT 1 FROM llm_project_model_assignments
           WHERE project_id = candidate_project_id AND tier = 'fast'
       )
       OR NOT EXISTS (
           SELECT 1 FROM llm_project_model_assignments
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
                 WHERE assignment.project_id = provider_policy.project_id
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

CREATE CONSTRAINT TRIGGER llm_vault_provider_credentials_validate_active_setup
AFTER UPDATE OR DELETE ON llm_vault_provider_credentials
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();
CREATE CONSTRAINT TRIGGER llm_vault_consumers_validate_active_setup
AFTER INSERT OR UPDATE OR DELETE ON llm_vault_connection_consumers
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION apdl_check_active_agents_setup_trigger();

-- apdl_runtime may inspect only the non-secret authority required by routing.
REVOKE ALL ON llm_vault_connections,
    llm_vault_provider_credentials,
    llm_vault_provider_secrets,
    llm_vault_connection_consumers,
    llm_vault_provider_models,
    llm_vault_audit,
    llm_vault_access_audit,
    llm_vault_key_rotation_audit
FROM PUBLIC, apdl_runtime;

GRANT SELECT ON llm_vault_connections TO apdl_runtime;
GRANT SELECT ON llm_vault_provider_credentials TO apdl_runtime;
GRANT SELECT ON llm_vault_connection_consumers TO apdl_runtime;
GRANT SELECT ON llm_vault_provider_models TO apdl_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    llm_vault_connections,
    llm_vault_provider_credentials,
    llm_vault_provider_secrets,
    llm_vault_connection_consumers,
    llm_vault_provider_models,
    llm_project_provider_connections,
    llm_project_provider_models,
    llm_project_provider_connection_audit,
    codegen_project_provider_connections,
    codegen_project_provider_models,
    codegen_project_provider_connection_audit
TO apdl_llm_vault;

GRANT SELECT, INSERT ON
    llm_vault_audit,
    llm_vault_access_audit,
    llm_vault_key_rotation_audit
TO apdl_llm_vault;

GRANT SELECT ON
    admin_projects,
    admin_users,
    admin_user_projects,
    llm_project_policies,
    llm_project_model_assignments,
    codegen_project_model_assignments
TO apdl_llm_vault;

GRANT UPDATE ON codegen_project_model_assignments TO apdl_llm_vault;

COMMENT ON TABLE llm_vault_connections IS
    'Canonical project provider connections with explicit Agents/Codegen grants.';
COMMENT ON TABLE llm_vault_provider_secrets IS
    'Vault-only AES-256-GCM ciphertext; runtime services have no privileges.';
COMMENT ON TABLE llm_vault_access_audit IS
    'Immutable just-in-time plaintext credential issuance evidence.';
COMMENT ON TABLE llm_vault_key_rotation_audit IS
    'Immutable evidence for offline, vault-wide encryption-key rotation.';
