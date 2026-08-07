-- Bind each LLM tier and provider egress attempt to exact project authority.

ALTER TABLE llm_project_provider_policies
    ADD CONSTRAINT llm_project_provider_policies_assignment_identity_key
    UNIQUE (project_id, provider, model, endpoint_url);

CREATE TABLE llm_project_model_assignments (
    project_id TEXT NOT NULL
        REFERENCES llm_project_policies(project_id) ON DELETE CASCADE,
    tier TEXT NOT NULL CHECK (tier IN ('fast', 'reasoning')),
    provider TEXT NOT NULL
        CHECK (provider IN ('openai', 'anthropic', 'google', 'xai', 'local')),
    model TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    assigned_by_actor TEXT NOT NULL
        CHECK (
            assigned_by_actor = BTRIM(assigned_by_actor)
            AND LENGTH(assigned_by_actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN assigned_by_actor) = 0
            AND POSITION(CHR(13) IN assigned_by_actor) = 0
        ),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT llm_project_model_assignments_pkey
        PRIMARY KEY (project_id, tier),
    CONSTRAINT llm_project_model_assignments_policy_fk
        FOREIGN KEY (project_id, provider, model, endpoint_url)
        REFERENCES llm_project_provider_policies (
            project_id, provider, model, endpoint_url
        )
        ON DELETE RESTRICT,
    CONSTRAINT llm_project_model_assignments_endpoint_check CHECK (
        LENGTH(endpoint_url) BETWEEN 8 AND 512
        AND endpoint_url ~ '^https?://[^[:space:]]+$'
        AND RIGHT(endpoint_url, 1) <> '/'
    )
);

CREATE INDEX llm_project_model_assignments_provider_idx
    ON llm_project_model_assignments (project_id, provider, model);

ALTER TABLE llm_project_provider_credentials
    ADD CONSTRAINT llm_project_provider_credentials_attempt_identity_key
    UNIQUE (credential_id, project_id, provider, credential_version);

ALTER TABLE llm_provider_attempts
    ADD COLUMN credential_id UUID,
    ADD COLUMN credential_version BIGINT;

ALTER TABLE llm_provider_attempts
    ADD CONSTRAINT llm_provider_attempts_credential_fk
    FOREIGN KEY (
        credential_id, project_id, provider, credential_version
    )
    REFERENCES llm_project_provider_credentials (
        credential_id, project_id, provider, credential_version
    )
    ON DELETE RESTRICT;

-- Terminal rows written before this migration are retained as explicit legacy
-- history. Every new prepared/in-flight remote attempt requires a project
-- credential binding; local execution never receives a cloud credential.
ALTER TABLE llm_provider_attempts
    ADD COLUMN legacy_unbound_credential BOOLEAN NOT NULL DEFAULT FALSE;

-- Migration quiescence stops Agents before applying this file, which can leave
-- prepared attempts with a live lease or in-flight attempts whose provider
-- outcome is unknown. Preserve the same conservative accounting used by the
-- runtime reconciler before requiring every live row to have a credential
-- binding: pre-egress reservations are released, while in-flight reservations
-- are charged in full.
WITH live_unbound_attempts AS (
    SELECT attempt_id, status AS previous_status
    FROM llm_provider_attempts
    WHERE provider <> 'local'
      AND credential_id IS NULL
      AND status IN ('prepared', 'in_flight')
    FOR UPDATE
)
UPDATE llm_provider_attempts AS attempt
SET status = CASE
        WHEN live.previous_status = 'prepared' THEN 'blocked'
        ELSE 'cancelled'
    END,
    charged_cost_usd_micros = CASE
        WHEN live.previous_status = 'prepared' THEN 0
        ELSE attempt.reserved_cost_usd_micros
    END,
    retryable = FALSE,
    error_classification = 'cancelled',
    error_message = CASE
        WHEN live.previous_status = 'prepared'
            THEN 'Migration cancelled attempt before provider egress'
        ELSE 'Migration cancelled attempt with provider outcome unknown'
    END,
    completed_at = NOW()
FROM live_unbound_attempts AS live
WHERE attempt.attempt_id = live.attempt_id;

UPDATE llm_provider_attempts
SET legacy_unbound_credential = TRUE
WHERE provider <> 'local'
  AND credential_id IS NULL
  AND status IN ('succeeded', 'failed', 'cancelled', 'blocked');

-- Keep the logical ledger consistent with the attempts terminalized above.
-- A call remains active only when another attempt is still active.
WITH affected_calls AS (
    SELECT call.call_id
    FROM llm_calls AS call
    WHERE call.status IN ('prepared', 'in_flight')
      AND EXISTS (
          SELECT 1
          FROM llm_provider_attempts AS legacy_attempt
          WHERE legacy_attempt.call_id = call.call_id
            AND legacy_attempt.legacy_unbound_credential
      )
      AND NOT EXISTS (
          SELECT 1
          FROM llm_provider_attempts AS active_attempt
          WHERE active_attempt.call_id = call.call_id
            AND active_attempt.status IN ('prepared', 'in_flight')
      )
    FOR UPDATE OF call
), totals AS (
    SELECT affected.call_id,
           COALESCE(SUM(attempt.input_tokens), 0)::INTEGER AS input_tokens,
           COALESCE(SUM(attempt.output_tokens), 0)::INTEGER AS output_tokens,
           COALESCE(SUM(attempt.charged_cost_usd_micros), 0)::BIGINT
               AS cost_usd_micros
    FROM affected_calls AS affected
    LEFT JOIN llm_provider_attempts AS attempt
      ON attempt.call_id = affected.call_id
    GROUP BY affected.call_id
)
UPDATE llm_calls AS call
SET status = 'cancelled',
    input_tokens = totals.input_tokens,
    output_tokens = totals.output_tokens,
    cost_usd_micros = totals.cost_usd_micros,
    error_classification = 'cancelled',
    error_message = 'Migration cancelled an active provider attempt',
    completed_at = NOW()
FROM totals
WHERE call.call_id = totals.call_id;

ALTER TABLE llm_provider_attempts
    ADD CONSTRAINT llm_provider_attempts_credential_binding_check CHECK (
        (
            provider = 'local'
            AND credential_id IS NULL
            AND credential_version IS NULL
            AND NOT legacy_unbound_credential
        )
        OR (
            provider <> 'local'
            AND credential_id IS NOT NULL
            AND credential_version IS NOT NULL
            AND credential_version > 0
            AND NOT legacy_unbound_credential
        )
        OR (
            provider <> 'local'
            AND status IN ('succeeded', 'failed', 'cancelled', 'blocked')
            AND credential_id IS NULL
            AND credential_version IS NULL
            AND legacy_unbound_credential
        )
    );

CREATE OR REPLACE FUNCTION apdl_protect_llm_attempt_credential_binding()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_protect_llm_attempt_credential_binding$
BEGIN
    IF (
        (TG_OP = 'INSERT' AND NEW.legacy_unbound_credential)
        OR (
            TG_OP = 'UPDATE'
            AND NEW.legacy_unbound_credential
            AND NOT OLD.legacy_unbound_credential
        )
        OR (
            TG_OP = 'UPDATE'
            AND (
                NEW.credential_id IS DISTINCT FROM OLD.credential_id
                OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
                OR NEW.project_id IS DISTINCT FROM OLD.project_id
                OR NEW.provider IS DISTINCT FROM OLD.provider
            )
        )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'LLM attempt credential binding is immutable';
    END IF;
    RETURN NEW;
END
$apdl_protect_llm_attempt_credential_binding$;

CREATE TRIGGER llm_provider_attempts_protect_credential_binding
BEFORE INSERT OR UPDATE OF
    credential_id, credential_version, project_id, provider,
    legacy_unbound_credential
ON llm_provider_attempts
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_llm_attempt_credential_binding();

ALTER TABLE llm_calls
    DROP CONSTRAINT llm_calls_error_classification_check,
    ADD CONSTRAINT llm_calls_error_classification_check
    CHECK (error_classification IS NULL OR error_classification IN (
        'timeout', 'network', 'rate_limited', 'provider_unavailable',
        'authentication', 'permission', 'invalid_request',
        'model_not_found', 'safety_block', 'policy_denied',
        'budget_exceeded', 'run_inactive', 'cost_overrun', 'no_provider',
        'credential_unavailable', 'cancelled', 'governance_unavailable',
        'unknown'
    ));

ALTER TABLE llm_provider_attempts
    DROP CONSTRAINT llm_provider_attempts_error_classification_check,
    ADD CONSTRAINT llm_provider_attempts_error_classification_check
    CHECK (error_classification IS NULL OR error_classification IN (
        'timeout', 'network', 'rate_limited', 'provider_unavailable',
        'authentication', 'permission', 'invalid_request',
        'model_not_found', 'safety_block', 'policy_denied',
        'budget_exceeded', 'run_inactive', 'cost_overrun', 'no_provider',
        'credential_unavailable', 'cancelled', 'governance_unavailable',
        'unknown'
    ));
