-- Migration 007: Canonical Codegen service storage.
--
-- Codegen previously ran this DDL during process startup. Keep upgrades and
-- legacy reconciliation here so application replicas only validate schemas.

CREATE TABLE IF NOT EXISTS codegen_connections (
    project_id TEXT PRIMARY KEY,
    installation_id BIGINT NOT NULL,
    repo TEXT NOT NULL,
    default_base_branch TEXT NOT NULL DEFAULT 'main',
    policy JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS codegen_changesets (
    changeset_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    base_branch TEXT,
    branch TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    head_sha TEXT,
    github_pr_status TEXT,
    external_ci_status TEXT,
    merge_sha TEXT,
    task JSONB NOT NULL DEFAULT '{}',
    diff_stat JSONB NOT NULL DEFAULT '{}',
    prompts JSONB NOT NULL DEFAULT '[]',
    contract_bundle JSONB,
    requirement_ledger JSONB,
    inspection_snapshot JSONB,
    dependency_slice JSONB,
    verification_plan JSONB,
    verification_coverage JSONB,
    runtime_acceptance_plan JSONB,
    runtime_evidence_assessment JSONB,
    review_verdict JSONB,
    publication_authorization JSONB,
    external_ci_awaiting_since TIMESTAMPTZ,
    ci_retry_count INTEGER NOT NULL DEFAULT 0,
    ci_remediation_status TEXT NOT NULL DEFAULT 'idle',
    ci_failure_key TEXT,
    ci_failure_summary TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS merge_sha TEXT;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS prompts JSONB NOT NULL DEFAULT '[]';
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS contract_bundle JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS requirement_ledger JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS inspection_snapshot JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS dependency_slice JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS verification_plan JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS verification_coverage JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS review_verdict JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS runtime_acceptance_plan JSONB;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS runtime_evidence_assessment JSONB;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS publication_authorization JSONB;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS head_sha TEXT;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS github_pr_status TEXT;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS external_ci_status TEXT;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS external_ci_awaiting_since TIMESTAMPTZ;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS ci_retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS ci_remediation_status TEXT NOT NULL DEFAULT 'idle';
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS ci_failure_key TEXT;
ALTER TABLE codegen_changesets ADD COLUMN IF NOT EXISTS ci_failure_summary TEXT;

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_status_check;
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_external_ci_status_check;
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_github_pr_status_check;
ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_ci_remediation_status_check;

-- Move values from superseded CI projection columns before dropping them.
DO $reconcile_codegen_changesets$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'codegen_changesets'
          AND column_name = 'ci_awaiting_since'
    ) THEN
        EXECUTE 'UPDATE codegen_changesets '
                'SET external_ci_awaiting_since = COALESCE('
                'external_ci_awaiting_since, ci_awaiting_since)';
        EXECUTE 'ALTER TABLE codegen_changesets DROP COLUMN ci_awaiting_since';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'codegen_changesets'
          AND column_name = 'ci_status'
    ) THEN
        -- Legacy rows have no immutable exact-head observation. Even an old
        -- "passed" projection must be reverified against GitHub live.
        EXECUTE 'UPDATE codegen_changesets '
                'SET external_ci_status = CASE '
                'WHEN pr_number IS NOT NULL THEN ''unverified_external_ci'' '
                'ELSE NULL END WHERE external_ci_status IS NULL';
        EXECUTE 'ALTER TABLE codegen_changesets DROP COLUMN ci_status';
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'codegen_changesets'
          AND column_name = 'pr_node_id'
    ) THEN
        EXECUTE 'ALTER TABLE codegen_changesets DROP COLUMN pr_node_id';
    END IF;
END
$reconcile_codegen_changesets$;

UPDATE codegen_changesets
SET status = CASE
    WHEN status = 'testing' THEN 'editing'
    WHEN status = 'tests_failed' THEN 'error'
    WHEN status IN (
        'ci_running', 'ci_failed', 'ci_passed',
        'unverified_external_ci', 'waiting_approval'
    ) THEN 'pr_open'
    ELSE status
END;
UPDATE codegen_changesets
SET external_ci_status = CASE
    WHEN pr_number IS NOT NULL THEN 'unverified_external_ci'
    ELSE NULL
END
WHERE external_ci_status IS NOT NULL
  AND external_ci_status NOT IN
      ('pending', 'passed', 'failed', 'unverified_external_ci');
UPDATE codegen_changesets
SET github_pr_status = NULL
WHERE github_pr_status IS NOT NULL
  AND github_pr_status NOT IN ('draft', 'open', 'merged', 'closed');
UPDATE codegen_changesets
SET ci_remediation_status = 'idle'
WHERE ci_remediation_status IS NULL
   OR ci_remediation_status NOT IN
      ('idle', 'diagnosing', 'repairing', 'awaiting_ci', 'resolved', 'exhausted');
UPDATE codegen_changesets SET ci_retry_count = 0 WHERE ci_retry_count IS NULL;
UPDATE codegen_changesets SET prompts = '[]'::jsonb WHERE prompts IS NULL;

ALTER TABLE codegen_changesets
    ALTER COLUMN ci_remediation_status SET DEFAULT 'idle',
    ALTER COLUMN ci_remediation_status SET NOT NULL,
    ALTER COLUMN ci_retry_count SET DEFAULT 0,
    ALTER COLUMN ci_retry_count SET NOT NULL,
    ALTER COLUMN prompts SET DEFAULT '[]'::jsonb,
    ALTER COLUMN prompts SET NOT NULL;
ALTER TABLE codegen_changesets ADD CONSTRAINT codegen_changesets_status_check
    CHECK (status IN (
        'queued', 'cloning', 'editing', 'pushing', 'pr_open',
        'merged', 'abandoned', 'error'
    ));
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_external_ci_status_check
    CHECK (external_ci_status IS NULL OR external_ci_status IN
           ('pending', 'passed', 'failed', 'unverified_external_ci'));
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_github_pr_status_check
    CHECK (github_pr_status IS NULL OR github_pr_status IN
           ('draft', 'open', 'merged', 'closed'));
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_ci_remediation_status_check
    CHECK (ci_remediation_status IN
           ('idle', 'diagnosing', 'repairing', 'awaiting_ci', 'resolved', 'exhausted'));

CREATE INDEX IF NOT EXISTS idx_codegen_changesets_project
    ON codegen_changesets (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS codegen_pull_request_observations (
    observation_id TEXT PRIMARY KEY,
    delivery_id TEXT,
    changeset_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    github_updated_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);
ALTER TABLE codegen_pull_request_observations
    ADD COLUMN IF NOT EXISTS github_updated_at TIMESTAMPTZ;
UPDATE codegen_pull_request_observations
SET github_updated_at = observed_at
WHERE github_updated_at IS NULL;
ALTER TABLE codegen_pull_request_observations
    ALTER COLUMN github_updated_at SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_codegen_pr_observation_delivery
    ON codegen_pull_request_observations (delivery_id)
    WHERE delivery_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_codegen_pr_observation_head
    ON codegen_pull_request_observations
       (changeset_id, head_sha, github_updated_at DESC, observed_at DESC);

CREATE TABLE IF NOT EXISTS codegen_ci_verification_observations (
    observation_id TEXT PRIMARY KEY,
    changeset_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (changeset_id, head_sha, evidence_hash)
);
CREATE INDEX IF NOT EXISTS idx_codegen_ci_observation_head
    ON codegen_ci_verification_observations
       (changeset_id, head_sha, observed_at DESC);

CREATE TABLE IF NOT EXISTS codegen_runtime_evidence_observations (
    observation_id TEXT PRIMARY KEY,
    changeset_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    ci_observation_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (changeset_id, head_sha, evidence_hash)
);
ALTER TABLE codegen_runtime_evidence_observations
    ADD COLUMN IF NOT EXISTS ci_observation_id TEXT;

-- Rows written before exact CI-observation binding cannot satisfy the current
-- evidence contract. Preserve them rather than inventing an authority link.
DO $preserve_unbound_runtime_evidence$
BEGIN
    IF EXISTS (
        SELECT 1 FROM codegen_runtime_evidence_observations
        WHERE ci_observation_id IS NULL
    ) THEN
        IF to_regclass(
            'public.codegen_runtime_evidence_observations_legacy_unbound'
        ) IS NOT NULL THEN
            RAISE EXCEPTION
                'Legacy unbound runtime-evidence backup already exists';
        END IF;
        CREATE TABLE codegen_runtime_evidence_observations_legacy_unbound AS
            SELECT * FROM codegen_runtime_evidence_observations
            WHERE ci_observation_id IS NULL;
        DELETE FROM codegen_runtime_evidence_observations
            WHERE ci_observation_id IS NULL;
    END IF;
END
$preserve_unbound_runtime_evidence$;
ALTER TABLE codegen_runtime_evidence_observations
    ALTER COLUMN ci_observation_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_codegen_runtime_evidence_head
    ON codegen_runtime_evidence_observations
       (changeset_id, head_sha, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_codegen_runtime_evidence_ci_observation
    ON codegen_runtime_evidence_observations
       (changeset_id, head_sha, ci_observation_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS codegen_runtime_collection_claims (
    changeset_id TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    ci_observation_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (changeset_id, head_sha, ci_observation_id)
);

CREATE TABLE IF NOT EXISTS codegen_ci_remediation_attempts (
    event_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    changeset_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    failed_head_sha TEXT NOT NULL,
    failure_observation_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (attempt_id, event_sequence)
);
CREATE INDEX IF NOT EXISTS idx_codegen_remediation_attempt_head
    ON codegen_ci_remediation_attempts
       (changeset_id, failed_head_sha, recorded_at DESC);

CREATE TABLE IF NOT EXISTS codegen_ci_remediation_claims (
    changeset_id TEXT NOT NULL,
    failed_head_sha TEXT NOT NULL,
    claim_scope TEXT NOT NULL,
    failure_observation_id TEXT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (changeset_id, failed_head_sha, claim_scope)
);
