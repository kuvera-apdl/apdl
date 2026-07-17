-- Replica-safe mutation quota reservations.  Every intended mutation carries a
-- stable idempotency key, so retries reuse one reservation while independent
-- replicas share the same rolling-hour project/action budget.
CREATE TABLE agent_mutation_quota_reservations (
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT agent_mutation_quota_reservations_pkey
        PRIMARY KEY (project_id, action_type, idempotency_key),
    CONSTRAINT agent_mutation_quota_project_id_check
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    CONSTRAINT agent_mutation_quota_action_type_check
        CHECK (action_type IN (
            'create_experiment',
            'update_flag',
            'update_ui_config',
            'feature_proposal',
            'open_pull_request'
    )),
    CONSTRAINT agent_mutation_quota_idempotency_key_check
        CHECK (
            char_length(idempotency_key) BETWEEN 1 AND 256
            AND btrim(idempotency_key) <> ''
        ),
    CONSTRAINT agent_mutation_quota_policy_version_check
        CHECK (policy_version = 'rolling_hour@1')
);

CREATE INDEX agent_mutation_quota_reservations_lookup_idx
    ON agent_mutation_quota_reservations
        (project_id, action_type, policy_version, occurred_at DESC);
