-- Canonical LLM governance contract.
--
-- The former llm_calls table mixed a logical request with one provider
-- attempt and had no runtime writer. Preserve any manually-written history,
-- then install separate logical-call and provider-attempt ledgers.
ALTER TABLE llm_calls RENAME TO llm_calls_legacy_pre_governance_023;
ALTER TABLE llm_calls_legacy_pre_governance_023
    RENAME CONSTRAINT llm_calls_pkey
    TO llm_calls_legacy_pre_governance_023_pkey;

CREATE TABLE llm_project_policies (
    project_id TEXT PRIMARY KEY
        REFERENCES admin_projects(project_id) ON DELETE CASCADE,
    required_data_residency TEXT NOT NULL DEFAULT 'local',
    allow_cross_vendor_retry BOOLEAN NOT NULL DEFAULT FALSE,
    project_daily_cost_limit_usd_micros BIGINT NOT NULL DEFAULT 0,
    run_cost_limit_usd_micros BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT llm_project_policy_residency_check
        CHECK (required_data_residency IN ('local', 'ca', 'us', 'eu', 'global')),
    CONSTRAINT llm_project_policy_cost_limits_check
        CHECK (
            project_daily_cost_limit_usd_micros >= 0
            AND run_cost_limit_usd_micros >= 0
        )
);

CREATE TABLE llm_project_provider_policies (
    project_id TEXT NOT NULL
        REFERENCES llm_project_policies(project_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    data_residency TEXT NOT NULL,
    allowed_data_classifications TEXT[] NOT NULL,
    input_cost_per_million_tokens_usd_micros BIGINT NOT NULL,
    output_cost_per_million_tokens_usd_micros BIGINT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT llm_project_provider_policies_pkey
        PRIMARY KEY (project_id, provider, model),
    CONSTRAINT llm_project_provider_name_check
        CHECK (provider IN ('openai', 'anthropic', 'google', 'local')),
    CONSTRAINT llm_project_provider_model_check
        CHECK (char_length(model) BETWEEN 1 AND 128 AND btrim(model) <> ''),
    CONSTRAINT llm_project_provider_endpoint_check
        CHECK (
            char_length(endpoint_url) BETWEEN 8 AND 512
            AND endpoint_url ~ '^https?://[^[:space:]]+$'
            AND right(endpoint_url, 1) <> '/'
        ),
    CONSTRAINT llm_project_provider_residency_check
        CHECK (data_residency IN ('local', 'ca', 'us', 'eu', 'global')),
    CONSTRAINT llm_project_provider_classifications_check
        CHECK (
            cardinality(allowed_data_classifications) BETWEEN 1 AND 4
            AND allowed_data_classifications <@ ARRAY[
                'public', 'internal', 'confidential', 'restricted'
            ]::TEXT[]
        ),
    CONSTRAINT llm_project_provider_cost_check
        CHECK (
            input_cost_per_million_tokens_usd_micros >= 0
            AND output_cost_per_million_tokens_usd_micros >= 0
            AND (
                provider = 'local'
                OR input_cost_per_million_tokens_usd_micros > 0
                OR output_cost_per_million_tokens_usd_micros > 0
            )
        ),
    CONSTRAINT llm_project_provider_local_residency_check
        CHECK (
            (provider = 'local' AND data_residency = 'local')
            OR (provider <> 'local' AND data_residency <> 'local')
        )
);

-- Safe default: only the explicitly configured local model may receive data,
-- no paid spend is admitted, and retries cannot cross a vendor boundary.
INSERT INTO llm_project_policies (project_id)
SELECT project_id FROM admin_projects
ON CONFLICT (project_id) DO NOTHING;

INSERT INTO llm_project_provider_policies (
    project_id,
    provider,
    model,
    endpoint_url,
    data_residency,
    allowed_data_classifications,
    input_cost_per_million_tokens_usd_micros,
    output_cost_per_million_tokens_usd_micros
)
SELECT
    project_id,
    'local',
    'gemma4',
    'http://localhost:11434/v1',
    'local',
    ARRAY['public', 'internal', 'confidential', 'restricted']::TEXT[],
    0,
    0
FROM admin_projects
ON CONFLICT (project_id, provider, model) DO NOTHING;

CREATE OR REPLACE FUNCTION ensure_llm_project_policy_defaults()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $llm_project_policy_defaults$
BEGIN
    INSERT INTO llm_project_policies (project_id)
    VALUES (NEW.project_id)
    ON CONFLICT (project_id) DO NOTHING;

    INSERT INTO llm_project_provider_policies (
        project_id,
        provider,
        model,
        endpoint_url,
        data_residency,
        allowed_data_classifications,
        input_cost_per_million_tokens_usd_micros,
        output_cost_per_million_tokens_usd_micros
    )
    VALUES (
        NEW.project_id,
        'local',
        'gemma4',
        'http://localhost:11434/v1',
        'local',
        ARRAY['public', 'internal', 'confidential', 'restricted']::TEXT[],
        0,
        0
    )
    ON CONFLICT (project_id, provider, model) DO NOTHING;
    RETURN NEW;
END
$llm_project_policy_defaults$;

CREATE TRIGGER admin_projects_ensure_llm_policy
AFTER INSERT ON admin_projects
FOR EACH ROW EXECUTE FUNCTION ensure_llm_project_policy_defaults();

CREATE TABLE llm_calls (
    call_id UUID NOT NULL DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    execution_kind TEXT NOT NULL,
    execution_owner_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    data_classification TEXT NOT NULL,
    prompt_sha256 CHAR(64) NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared',
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd_micros BIGINT NOT NULL DEFAULT 0,
    error_classification TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT llm_calls_governed_pkey PRIMARY KEY (call_id),
    CONSTRAINT llm_calls_governed_execution_identity
        UNIQUE (project_id, run_id, call_id, execution_owner_id),
    CONSTRAINT llm_calls_run_id_check
        CHECK (char_length(run_id) BETWEEN 1 AND 128 AND btrim(run_id) <> ''),
    CONSTRAINT llm_calls_execution_kind_check
        CHECK (execution_kind IN ('agent_run', 'custom_agent_test')),
    CONSTRAINT llm_calls_execution_owner_check
        CHECK (
            char_length(execution_owner_id) BETWEEN 1 AND 512
            AND btrim(execution_owner_id) <> ''
            AND execution_owner_id !~ '[[:space:]]'
        ),
    CONSTRAINT llm_calls_purpose_check
        CHECK (
            char_length(purpose) BETWEEN 1 AND 128
            AND purpose ~ '^[a-z][a-z0-9_.:-]*$'
        ),
    CONSTRAINT llm_calls_data_classification_check
        CHECK (data_classification IN (
            'public', 'internal', 'confidential', 'restricted'
        )),
    CONSTRAINT llm_calls_prompt_sha256_check
        CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT llm_calls_status_check
        CHECK (status IN (
            'prepared', 'in_flight', 'succeeded', 'failed', 'cancelled', 'blocked'
        )),
    CONSTRAINT llm_calls_counts_check
        CHECK (
            attempt_count BETWEEN 0 AND 16
            AND input_tokens >= 0
            AND output_tokens >= 0
            AND cost_usd_micros >= 0
        ),
    CONSTRAINT llm_calls_error_message_check
        CHECK (error_message IS NULL OR char_length(error_message) <= 4000),
    CONSTRAINT llm_calls_error_classification_check
        CHECK (error_classification IS NULL OR error_classification IN (
            'timeout', 'network', 'rate_limited', 'provider_unavailable',
            'authentication', 'permission', 'invalid_request',
            'model_not_found', 'safety_block', 'policy_denied',
            'budget_exceeded', 'run_inactive', 'cost_overrun', 'no_provider',
            'cancelled', 'governance_unavailable', 'unknown'
        )),
    CONSTRAINT llm_calls_completion_check
        CHECK (
            (status IN ('prepared', 'in_flight')
                AND completed_at IS NULL
                AND error_classification IS NULL)
            OR (status IN ('succeeded', 'failed', 'cancelled', 'blocked')
                AND completed_at IS NOT NULL
                AND (
                    (status = 'succeeded' AND error_classification IS NULL)
                    OR (status <> 'succeeded' AND error_classification IS NOT NULL)
                ))
        )
);

CREATE INDEX llm_calls_project_created_idx
    ON llm_calls (project_id, created_at DESC);
CREATE INDEX llm_calls_run_created_idx
    ON llm_calls (project_id, run_id, created_at DESC);

CREATE TABLE llm_provider_attempts (
    attempt_id UUID NOT NULL DEFAULT gen_random_uuid(),
    call_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    attempt_number SMALLINT NOT NULL,
    execution_owner_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    endpoint_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared',
    prompt_sha256 CHAR(64) NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    reserved_cost_usd_micros BIGINT NOT NULL,
    charged_cost_usd_micros BIGINT,
    latency_ms INTEGER,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    error_classification TEXT,
    error_message TEXT,
    prepared_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    egress_started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT llm_provider_attempts_pkey PRIMARY KEY (attempt_id),
    CONSTRAINT llm_provider_attempts_call_fk
        FOREIGN KEY (project_id, run_id, call_id, execution_owner_id)
        REFERENCES llm_calls(
            project_id, run_id, call_id, execution_owner_id
        ) ON DELETE CASCADE,
    CONSTRAINT llm_provider_attempts_call_order_key
        UNIQUE (call_id, attempt_number),
    CONSTRAINT llm_provider_attempts_run_id_check
        CHECK (char_length(run_id) BETWEEN 1 AND 128 AND btrim(run_id) <> ''),
    CONSTRAINT llm_provider_attempts_number_check
        CHECK (attempt_number BETWEEN 1 AND 16),
    CONSTRAINT llm_provider_attempts_execution_owner_check
        CHECK (
            char_length(execution_owner_id) BETWEEN 1 AND 512
            AND btrim(execution_owner_id) <> ''
            AND execution_owner_id !~ '[[:space:]]'
        ),
    CONSTRAINT llm_provider_attempts_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google', 'local')),
    CONSTRAINT llm_provider_attempts_model_check
        CHECK (char_length(model) BETWEEN 1 AND 128 AND btrim(model) <> ''),
    CONSTRAINT llm_provider_attempts_endpoint_check
        CHECK (
            char_length(endpoint_url) BETWEEN 8 AND 512
            AND endpoint_url ~ '^https?://[^[:space:]]+$'
            AND right(endpoint_url, 1) <> '/'
        ),
    CONSTRAINT llm_provider_attempts_status_check
        CHECK (status IN (
            'prepared', 'in_flight', 'succeeded', 'failed', 'cancelled', 'blocked'
        )),
    CONSTRAINT llm_provider_attempts_prompt_sha256_check
        CHECK (prompt_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT llm_provider_attempts_usage_check
        CHECK (
            estimated_input_tokens >= 0
            AND max_output_tokens > 0
            AND (input_tokens IS NULL OR input_tokens >= 0)
            AND (output_tokens IS NULL OR output_tokens >= 0)
            AND reserved_cost_usd_micros >= 0
            AND (charged_cost_usd_micros IS NULL OR charged_cost_usd_micros >= 0)
            AND (latency_ms IS NULL OR latency_ms >= 0)
        ),
    CONSTRAINT llm_provider_attempts_error_message_check
        CHECK (error_message IS NULL OR char_length(error_message) <= 4000),
    CONSTRAINT llm_provider_attempts_error_classification_check
        CHECK (error_classification IS NULL OR error_classification IN (
            'timeout', 'network', 'rate_limited', 'provider_unavailable',
            'authentication', 'permission', 'invalid_request',
            'model_not_found', 'safety_block', 'policy_denied',
            'budget_exceeded', 'run_inactive', 'cost_overrun', 'no_provider',
            'cancelled', 'governance_unavailable', 'unknown'
        )),
    CONSTRAINT llm_provider_attempts_lifecycle_check
        CHECK (
            (status = 'prepared'
                AND egress_started_at IS NULL
                AND completed_at IS NULL
                AND charged_cost_usd_micros IS NULL
                AND error_classification IS NULL)
            OR (status = 'in_flight'
                AND egress_started_at IS NOT NULL
                AND completed_at IS NULL
                AND charged_cost_usd_micros IS NULL
                AND error_classification IS NULL)
            OR (status IN ('succeeded', 'failed', 'cancelled')
                AND egress_started_at IS NOT NULL
                AND completed_at IS NOT NULL
                AND charged_cost_usd_micros IS NOT NULL
                AND (
                    (status = 'succeeded' AND error_classification IS NULL)
                    OR (status <> 'succeeded' AND error_classification IS NOT NULL)
                ))
            OR (status = 'blocked'
                AND egress_started_at IS NULL
                AND completed_at IS NOT NULL
                AND charged_cost_usd_micros = 0
                AND error_classification IS NOT NULL)
        )
);

CREATE INDEX llm_provider_attempts_project_budget_idx
    ON llm_provider_attempts (project_id, prepared_at DESC);
CREATE INDEX llm_provider_attempts_run_budget_idx
    ON llm_provider_attempts (project_id, run_id, prepared_at DESC);
CREATE INDEX llm_provider_attempts_provider_idx
    ON llm_provider_attempts (project_id, provider, model, prepared_at DESC);
