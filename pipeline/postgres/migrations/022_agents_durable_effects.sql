-- Migration 022: durable agent phase results and approval effect outbox.
--
-- Human approval is a command, not an invitation to perform Config/Codegen
-- HTTP calls inside the request.  The command, its exact decisions, required
-- audit records, and ordered effects are committed together.  Every replica
-- may then lease and retry effects with their stable downstream identity.

ALTER TABLE agent_run_results
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}';

-- Ephemeral Admin API credentials carry the authenticated human identity as a
-- durable snapshot source. It is intentionally not a foreign key: deleting a
-- user must not erase who authorized a historical command.
ALTER TABLE auth_credentials
    ADD COLUMN IF NOT EXISTS actor_user_id UUID;

-- The dispatcher accepts one canonical config shape.  Runs created before the
-- durable queue stored this field implicitly as seven days.
UPDATE agent_runs
SET config = jsonb_set(COALESCE(config, '{}'::jsonb), '{time_range_days}', '7'::jsonb, true)
WHERE NOT (COALESCE(config, '{}'::jsonb) ? 'time_range_days');

CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_identity_project_idx
    ON agent_runs (run_id, project_id);

CREATE TABLE IF NOT EXISTS agent_approval_commands (
    command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    actor_credential_id TEXT NOT NULL
        CHECK (actor_credential_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    actor_user_id UUID,
    request_sha256 CHAR(64) NOT NULL
        CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    gate_id TEXT NOT NULL,
    gate_agent TEXT NOT NULL
        CHECK (gate_agent IN ('experiment_design', 'feature_proposal', 'code_implementation')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'processing', 'succeeded', 'manual_intervention')),
    resume_status TEXT NOT NULL
        CHECK (resume_status IN ('approved', 'rejected')),
    approved_count INTEGER NOT NULL CHECK (approved_count >= 0),
    rejected_count INTEGER NOT NULL CHECK (rejected_count >= 0),
    comment TEXT CHECK (comment IS NULL OR char_length(comment) <= 2000),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT agent_approval_commands_run_project_fk
        FOREIGN KEY (run_id, project_id)
        REFERENCES agent_runs(run_id, project_id) ON DELETE CASCADE,
    CONSTRAINT agent_approval_commands_effect_parent_unique
        UNIQUE (command_id, run_id, project_id),
    CONSTRAINT agent_approval_commands_gate_unique UNIQUE (run_id, gate_id),
    CONSTRAINT agent_approval_commands_request_unique UNIQUE (run_id, request_sha256),
    CONSTRAINT agent_approval_commands_decision_count_check
        CHECK (approved_count + rejected_count BETWEEN 1 AND 100),
    CONSTRAINT agent_approval_commands_admin_actor_check
        CHECK (
            actor_credential_id !~ '^adminproxy-'
            OR actor_user_id IS NOT NULL
        )
);

CREATE TABLE IF NOT EXISTS agent_approval_decisions (
    command_id UUID NOT NULL
        REFERENCES agent_approval_commands(command_id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    approved BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (command_id, item_id),
    CONSTRAINT agent_approval_decisions_item_id_check CHECK (
        char_length(item_id) BETWEEN 1 AND 128
        AND item_id = btrim(item_id)
    )
);

CREATE TABLE IF NOT EXISTS agent_approval_effects (
    effect_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    command_id UUID NOT NULL,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    effect_type TEXT NOT NULL CHECK (
        effect_type IN (
            'stage_experiment_draft',
            'open_treatment_changeset',
            'open_code_changeset',
            'record_experiment_rejection',
            'record_proposal_rejection',
            'quarantine_feature_proposal'
        )
    ),
    effect_order INTEGER NOT NULL CHECK (effect_order >= 0),
    depends_on_effect_id UUID,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN (
            'queued', 'processing', 'retryable_failed', 'succeeded',
            'failed', 'manual_intervention'
        )
    ),
    idempotency_key TEXT NOT NULL,
    quota_action_type TEXT CHECK (
        quota_action_type IS NULL
        OR quota_action_type IN ('create_experiment', 'open_pull_request')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 100),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner_id TEXT,
    lease_expires_at TIMESTAMPTZ,
    result JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT agent_approval_effects_command_project_fk
        FOREIGN KEY (command_id, run_id, project_id)
        REFERENCES agent_approval_commands(command_id, run_id, project_id)
        ON DELETE CASCADE,
    CONSTRAINT agent_approval_effects_dependency_parent_unique
        UNIQUE (command_id, effect_id),
    CONSTRAINT agent_approval_effects_dependency_command_fk
        FOREIGN KEY (command_id, depends_on_effect_id)
        REFERENCES agent_approval_effects(command_id, effect_id)
        ON DELETE RESTRICT,
    CONSTRAINT agent_approval_effects_identity_unique
        UNIQUE (command_id, item_id, effect_type),
    CONSTRAINT agent_approval_effects_idempotency_unique UNIQUE (idempotency_key),
    CONSTRAINT agent_approval_effects_item_id_check CHECK (
        char_length(item_id) BETWEEN 1 AND 128
        AND item_id = btrim(item_id)
    ),
    CONSTRAINT agent_approval_effects_idempotency_key_check CHECK (
        idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$'
    ),
    CONSTRAINT agent_approval_effects_lease_check CHECK (
        (lease_owner_id IS NULL) = (lease_expires_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS agent_approval_effects_dispatch_idx
    ON agent_approval_effects (next_attempt_at, created_at)
    WHERE status IN ('queued', 'retryable_failed', 'processing');
CREATE INDEX IF NOT EXISTS agent_approval_effects_command_idx
    ON agent_approval_effects (command_id, effect_order, created_at);
CREATE INDEX IF NOT EXISTS agent_approval_commands_run_idx
    ON agent_approval_commands (run_id, created_at DESC);

-- Config persists the same outbox identity it receives.  Manual/non-agent
-- experiment creation remains valid with NULL; retries carrying a key return
-- the originally created experiment instead of attempting a second bundle.
ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS creation_idempotency_key TEXT;
ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS creation_idempotency_request_sha256 CHAR(64);
ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_creation_idempotency_key_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_creation_idempotency_key_check CHECK (
        creation_idempotency_key IS NULL
        OR creation_idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$'
    );
ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_creation_idempotency_pair_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_creation_idempotency_pair_check CHECK (
        (
            creation_idempotency_key IS NULL
            AND creation_idempotency_request_sha256 IS NULL
        )
        OR (
            creation_idempotency_key IS NOT NULL
            AND creation_idempotency_request_sha256 ~ '^[0-9a-f]{64}$'
        )
    );
CREATE UNIQUE INDEX IF NOT EXISTS experiments_project_creation_idempotency_key_idx
    ON experiments (project_id, creation_idempotency_key)
    WHERE creation_idempotency_key IS NOT NULL;

-- Stable downstream identity for Codegen retries.  Existing rows receive a
-- deterministic legacy identity; all new callers must supply the canonical
-- key on the first request.
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS idempotency_request_sha256 CHAR(64);

UPDATE codegen_changesets
SET idempotency_key = 'legacy:'
        || md5(project_id || E'\x1f' || changeset_id)
        || md5('second:' || project_id || E'\x1f' || changeset_id),
    idempotency_request_sha256 = md5(
        'legacy-request:' || project_id || E'\x1f' || changeset_id
    ) || md5(
        'legacy-request-second:' || project_id || E'\x1f' || changeset_id
    )
WHERE idempotency_key IS NULL;

UPDATE codegen_changesets
SET idempotency_request_sha256 = md5(
        'legacy-request:' || project_id || E'\x1f' || changeset_id
    ) || md5(
        'legacy-request-second:' || project_id || E'\x1f' || changeset_id
    )
WHERE idempotency_request_sha256 IS NULL;

ALTER TABLE codegen_changesets
    ALTER COLUMN idempotency_key SET NOT NULL,
    ALTER COLUMN idempotency_request_sha256 SET NOT NULL;

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_idempotency_key_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_idempotency_key_check CHECK (
        idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$'
    );
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_idempotency_request_sha256_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_idempotency_request_sha256_check CHECK (
        idempotency_request_sha256 ~ '^[0-9a-f]{64}$'
    );

CREATE UNIQUE INDEX IF NOT EXISTS codegen_changesets_project_idempotency_key_idx
    ON codegen_changesets (project_id, idempotency_key);

-- Migration 015's legacy JSON backfill could associate a retry child with a
-- parent from another project because it joined only on the globally unique
-- changeset id. Never preserve that ambiguous tenant relationship silently.
DO $codegen_retry_tenant_check$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM codegen_changesets AS child
        JOIN codegen_changesets AS parent
          ON parent.changeset_id = child.retry_of_changeset_id
        WHERE child.project_id <> parent.project_id
    ) THEN
        RAISE EXCEPTION
            'codegen retry lineage crosses project boundaries; repair before migration 022';
    END IF;
END
$codegen_retry_tenant_check$;

CREATE UNIQUE INDEX IF NOT EXISTS codegen_changesets_project_identity_idx
    ON codegen_changesets (project_id, changeset_id);
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_retry_of_changeset_id_fkey;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_retry_of_changeset_id_fkey
    FOREIGN KEY (project_id, retry_of_changeset_id)
    REFERENCES codegen_changesets(project_id, changeset_id)
    ON DELETE RESTRICT;
