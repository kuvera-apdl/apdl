-- Migration 006: Canonical Config service storage.
--
-- Config used to create and alter these tables during every service startup.
-- This migration preserves the same live contract while making schema changes
-- an explicit, checksummed deployment step.

CREATE TABLE IF NOT EXISTS flags (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft', 'active', 'disabled', 'archived')),
    owners JSONB NOT NULL DEFAULT '[]'::jsonb,
    review_by TEXT,
    enabled BOOLEAN NOT NULL DEFAULT false,
    description TEXT NOT NULL DEFAULT '',
    default_variant TEXT NOT NULL DEFAULT 'control',
    variants JSONB NOT NULL DEFAULT
        '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb,
    rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    fallthrough JSONB NOT NULL DEFAULT
        '{"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb,
    salt TEXT NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text),
    evaluation_mode TEXT NOT NULL DEFAULT 'client'
        CHECK (evaluation_mode IN ('client', 'server', 'both')),
    auto_disable BOOLEAN NOT NULL DEFAULT true,
    guardrails JSONB NOT NULL DEFAULT '[]'::jsonb,
    disabled_reason TEXT NOT NULL DEFAULT '',
    disabled_by TEXT NOT NULL DEFAULT '',
    disabled_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT flags_default_variant_non_empty_check CHECK (
        default_variant <> ''
    ),
    CONSTRAINT flags_variants_array_non_empty_check CHECK (
        CASE
            WHEN jsonb_typeof(variants) <> 'array' THEN false
            ELSE jsonb_array_length(variants) > 0
        END
    ),
    CONSTRAINT flags_rules_array_check CHECK (jsonb_typeof(rules) = 'array'),
    CONSTRAINT flags_fallthrough_rollout_only_check CHECK (
        CASE
            WHEN jsonb_typeof(fallthrough) <> 'object' THEN false
            ELSE (fallthrough ? 'rollout')
                AND (fallthrough - 'rollout') = '{}'::jsonb
        END
    ),
    CONSTRAINT flags_state_enabled_check CHECK ((state = 'active') = enabled),
    PRIMARY KEY (project_id, key)
);

-- Reconcile tables produced by older Config builds before enforcing the strict
-- canonical shape. All newly required columns receive deterministic defaults.
ALTER TABLE flags ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS owners JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS review_by TEXT;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS default_variant TEXT NOT NULL DEFAULT 'control';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS variants JSONB NOT NULL DEFAULT
    '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS rules JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS fallthrough JSONB NOT NULL DEFAULT
    '{"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS salt TEXT NOT NULL DEFAULT
    md5(random()::text || clock_timestamp()::text);
ALTER TABLE flags ADD COLUMN IF NOT EXISTS evaluation_mode TEXT NOT NULL DEFAULT 'client';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS auto_disable BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS guardrails JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_by TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE flags ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE flags ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

-- Older lifecycle checks can reject the canonical values while rows are being
-- normalized. Remove them before any UPDATE and reinstall strict checks below.
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_state_check;
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_state_enabled_check;

UPDATE flags SET name = key WHERE name = '';
UPDATE flags
SET salt = md5(random()::text || clock_timestamp()::text || project_id || key)
WHERE salt = '';
UPDATE flags
SET default_variant = 'control'
WHERE default_variant IS NULL OR default_variant = '';
UPDATE flags
SET variants =
    '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb
WHERE CASE
    WHEN variants IS NULL THEN true
    WHEN jsonb_typeof(variants) <> 'array' THEN true
    ELSE jsonb_array_length(variants) = 0
END;
UPDATE flags
SET default_variant = 'control',
    variants =
        '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb
WHERE EXISTS (
        SELECT 1
        FROM jsonb_array_elements(variants) AS variant(value)
        WHERE jsonb_typeof(variant.value) <> 'object'
           OR COALESCE(variant.value->>'key', '') = ''
           OR COALESCE(variant.value->>'weight', '') !~ '^(0|[1-9][0-9]*)$'
    )
   OR NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(variants) AS variant(value)
        WHERE variant.value->>'key' = default_variant
    )
   OR (
        SELECT count(*) FROM jsonb_array_elements(variants) AS variant(value)
    ) <> (
        SELECT count(DISTINCT variant.value->>'key')
        FROM jsonb_array_elements(variants) AS variant(value)
    )
   OR COALESCE((
        SELECT sum((variant.value->>'weight')::numeric)
        FROM jsonb_array_elements(variants) AS variant(value)
        WHERE COALESCE(variant.value->>'weight', '') ~ '^(0|[1-9][0-9]*)$'
    ), 0) <= 0;
UPDATE flags
SET rules = '[]'::jsonb
WHERE rules IS NULL OR jsonb_typeof(rules) <> 'array';
UPDATE flags
SET fallthrough = jsonb_build_object(
    'rollout',
    CASE
        WHEN fallthrough IS NOT NULL
         AND jsonb_typeof(fallthrough) = 'object'
         AND jsonb_typeof(fallthrough->'rollout') = 'object'
        THEN fallthrough->'rollout'
        ELSE jsonb_build_object('percentage', 0, 'bucket_by', 'user_id')
    END
)
WHERE CASE
    WHEN fallthrough IS NULL THEN true
    WHEN jsonb_typeof(fallthrough) <> 'object' THEN true
    WHEN NOT (fallthrough ? 'rollout') THEN true
    ELSE (fallthrough - 'rollout') <> '{}'::jsonb
END;
UPDATE flags
SET state = CASE
    WHEN archived_at IS NOT NULL THEN 'archived'
    WHEN disabled_at IS NOT NULL OR disabled_reason <> '' THEN 'disabled'
    WHEN enabled THEN 'active'
    ELSE 'draft'
END
WHERE state = 'draft'
  AND (archived_at IS NOT NULL OR disabled_at IS NOT NULL
       OR disabled_reason <> '' OR enabled);
UPDATE flags
SET enabled = (state = 'active')
WHERE enabled IS DISTINCT FROM (state = 'active');

ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_evaluation_mode_check;
ALTER TABLE flags ADD CONSTRAINT flags_evaluation_mode_check
    CHECK (evaluation_mode IN ('client', 'server', 'both'));
ALTER TABLE flags ADD CONSTRAINT flags_state_check
    CHECK (state IN ('draft', 'active', 'disabled', 'archived'));
ALTER TABLE flags ADD CONSTRAINT flags_state_enabled_check
    CHECK ((state = 'active') = enabled);
ALTER TABLE flags ALTER COLUMN default_variant SET DEFAULT 'control';
ALTER TABLE flags ALTER COLUMN default_variant SET NOT NULL;
ALTER TABLE flags ALTER COLUMN variants SET DEFAULT
    '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb;
ALTER TABLE flags ALTER COLUMN variants SET NOT NULL;
ALTER TABLE flags ALTER COLUMN rules SET DEFAULT '[]'::jsonb;
ALTER TABLE flags ALTER COLUMN rules SET NOT NULL;
ALTER TABLE flags ALTER COLUMN fallthrough SET DEFAULT
    '{"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb;
ALTER TABLE flags ALTER COLUMN fallthrough SET NOT NULL;
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_default_variant_non_empty_check;
ALTER TABLE flags ADD CONSTRAINT flags_default_variant_non_empty_check
    CHECK (default_variant <> '');
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_variants_array_non_empty_check;
ALTER TABLE flags ADD CONSTRAINT flags_variants_array_non_empty_check CHECK (
    CASE
        WHEN jsonb_typeof(variants) <> 'array' THEN false
        ELSE jsonb_array_length(variants) > 0
    END
);
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_rules_array_check;
ALTER TABLE flags ADD CONSTRAINT flags_rules_array_check
    CHECK (jsonb_typeof(rules) = 'array');
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_fallthrough_rollout_only_check;
ALTER TABLE flags ADD CONSTRAINT flags_fallthrough_rollout_only_check CHECK (
    CASE
        WHEN jsonb_typeof(fallthrough) <> 'object' THEN false
        ELSE (fallthrough ? 'rollout')
            AND (fallthrough - 'rollout') = '{}'::jsonb
    END
);

-- Obsolete boolean/alias columns are deliberately removed. The service and
-- SDK expose one canonical variant contract after this migration.
ALTER TABLE flags DROP COLUMN IF EXISTS default_value;
ALTER TABLE flags DROP COLUMN IF EXISTS variant_type;
ALTER TABLE flags DROP COLUMN IF EXISTS rules_json;
ALTER TABLE flags DROP COLUMN IF EXISTS variants_json;
ALTER TABLE flags DROP COLUMN IF EXISTS rollout_percentage;
ALTER TABLE flags DROP COLUMN IF EXISTS client_exposed;

-- A pre-canonical `feature_flags` table cannot be safely projected into the
-- strict variant contract. Preserve it explicitly for operator-led recovery.
DO $preserve_legacy_feature_flags$
BEGIN
    IF to_regclass('public.feature_flags') IS NOT NULL THEN
        IF to_regclass('public.feature_flags_legacy') IS NOT NULL THEN
            RAISE EXCEPTION
                'Both feature_flags and feature_flags_legacy contain legacy data';
        END IF;
        ALTER TABLE public.feature_flags RENAME TO feature_flags_legacy;
    END IF;
END
$preserve_legacy_feature_flags$;

CREATE TABLE IF NOT EXISTS flag_audit_log (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    flag_key TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    previous_version INTEGER,
    new_version INTEGER,
    before JSONB,
    after JSONB,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE flag_audit_log
    ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS experiments (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'running', 'completed', 'stopped')),
    description TEXT NOT NULL DEFAULT '',
    flag_key TEXT NOT NULL DEFAULT '',
    default_variant TEXT NOT NULL DEFAULT 'control',
    variants_json TEXT NOT NULL DEFAULT '[]',
    targeting_rules_json TEXT NOT NULL DEFAULT '[]',
    primary_metric_json TEXT NOT NULL DEFAULT '{}',
    traffic_percentage DOUBLE PRECISION NOT NULL DEFAULT 100.0,
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, key)
);
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS flag_key TEXT NOT NULL DEFAULT '';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS default_variant TEXT NOT NULL DEFAULT 'control';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS variants_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS targeting_rules_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS primary_metric_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS traffic_percentage DOUBLE PRECISION NOT NULL DEFAULT 100.0;
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS start_date TEXT NOT NULL DEFAULT '';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS end_date TEXT NOT NULL DEFAULT '';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- The legacy check admitted `active` but rejected the canonical `running`, so
-- it must be removed before rewriting those rows.
ALTER TABLE experiments DROP CONSTRAINT IF EXISTS experiments_status_check;
UPDATE experiments SET flag_key = key WHERE flag_key = '';
UPDATE experiments SET status = 'running' WHERE status = 'active';
UPDATE experiments SET status = 'draft'
WHERE status NOT IN ('draft', 'running', 'completed', 'stopped');
ALTER TABLE experiments ADD CONSTRAINT experiments_status_check
    CHECK (status IN ('draft', 'running', 'completed', 'stopped'));

CREATE INDEX IF NOT EXISTS idx_flags_project_updated
    ON flags (project_id, archived_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_flags_project_state_review
    ON flags (project_id, state, review_by, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_flag_audit_project_flag
    ON flag_audit_log (project_id, flag_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_experiments_project_updated
    ON experiments (project_id, updated_at DESC);
