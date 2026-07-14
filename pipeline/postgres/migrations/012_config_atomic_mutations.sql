-- Migration 012: Atomic Config mutation authority.
--
-- Experiment/flag ownership, audit provenance, and delivery intent must be
-- committed together. Config publishes cache, SSE, and exposure side effects
-- from the durable outbox after the authoritative PostgreSQL transaction.

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;

ALTER TABLE experiments
    ADD CONSTRAINT experiments_version_check CHECK (version >= 1);

-- Do not let PostgreSQL interpret legacy date-only or local-time strings in the
-- database session timezone. Empty values become NULL; every nonempty value
-- must declare UTC (Z) or an explicit numeric offset before PostgreSQL parses
-- it. Any other malformed value then aborts the migration during the cast.
DO $validate_experiment_timestamps$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE (start_date <> ''
               AND start_date !~ '(Z|[+-][0-9]{2}:[0-9]{2})$')
           OR (end_date <> ''
               AND end_date !~ '(Z|[+-][0-9]{2}:[0-9]{2})$')
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiment dates: nonempty timestamps require an explicit timezone';
    END IF;
END
$validate_experiment_timestamps$;

ALTER TABLE experiments
    ALTER COLUMN start_date DROP DEFAULT,
    ALTER COLUMN end_date DROP DEFAULT,
    ALTER COLUMN start_date DROP NOT NULL,
    ALTER COLUMN end_date DROP NOT NULL;

ALTER TABLE experiments
    ALTER COLUMN start_date TYPE TIMESTAMPTZ
        USING NULLIF(start_date, '')::TIMESTAMPTZ,
    ALTER COLUMN end_date TYPE TIMESTAMPTZ
        USING NULLIF(end_date, '')::TIMESTAMPTZ;

-- An exact-prefix database may contain lifecycle rows authored before these
-- contracts existed. Refuse to reinterpret or silently repair them: operators
-- must reconcile the rows explicitly before retrying this migration.
DO $validate_experiment_lifecycle$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE status IN ('scheduled', 'running')
          AND (start_date IS NULL OR end_date IS NULL OR end_date <= start_date)
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiment lifecycle: scheduled/running rows require an ordered start and end';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE status = 'scheduled'
          AND start_date <= now()
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiment lifecycle: scheduled rows require a future start';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE status = 'running'
          AND (start_date > now() OR end_date <= now())
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiment lifecycle: running rows must contain the current time';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE status IN ('scheduled', 'running')
          AND CASE
              WHEN btrim(primary_metric_json) = '' THEN true
              ELSE
                  jsonb_typeof(primary_metric_json::jsonb) IS DISTINCT FROM 'object'
                  OR NOT (primary_metric_json::jsonb ? 'event')
                  OR jsonb_typeof(primary_metric_json::jsonb -> 'event')
                     IS DISTINCT FROM 'string'
                  OR btrim(primary_metric_json::jsonb ->> 'event') = ''
          END
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiment lifecycle: scheduled/running rows require a primary metric event';
    END IF;
END
$validate_experiment_lifecycle$;

ALTER TABLE experiments DROP CONSTRAINT IF EXISTS experiments_status_check;
ALTER TABLE experiments ADD CONSTRAINT experiments_status_check
    CHECK (status IN ('draft', 'scheduled', 'running', 'completed', 'stopped'));

ALTER TABLE experiments ADD CONSTRAINT experiments_date_window_check CHECK (
    end_date IS NULL OR (start_date IS NOT NULL AND end_date > start_date)
);

ALTER TABLE flag_audit_log
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'manual';

ALTER TABLE flag_audit_log
    ADD CONSTRAINT flag_audit_log_origin_check CHECK (
        origin IN ('manual', 'automation', 'experiment', 'scheduler')
    );

-- Refuse to invent ownership for inconsistent legacy data. The supported OSS
-- preview is a fresh install; an operator bringing forward older data must
-- reconcile orphaned or shared backing flags before applying this migration.
DO $validate_experiment_flag_ownership$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM experiments AS experiment
        LEFT JOIN flags AS flag
          ON flag.project_id = experiment.project_id
         AND flag.key = experiment.flag_key
        WHERE flag.key IS NULL
    ) THEN
        RAISE EXCEPTION
            'Cannot establish experiment ownership: an experiment references a missing backing flag';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM experiments
        GROUP BY project_id, flag_key
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot establish experiment ownership: a backing flag is shared by multiple experiments';
    END IF;
END
$validate_experiment_flag_ownership$;

ALTER TABLE experiments
    ADD CONSTRAINT experiments_flag_key_unique UNIQUE (project_id, flag_key);

ALTER TABLE experiments
    ADD CONSTRAINT experiments_flag_key_fkey
    FOREIGN KEY (project_id, flag_key)
    REFERENCES flags (project_id, key)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION reject_experiment_flag_ownership_change()
RETURNS trigger
LANGUAGE plpgsql
AS $immutable_experiment_flag_ownership$
BEGIN
    IF NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.flag_key IS DISTINCT FROM OLD.flag_key THEN
        RAISE EXCEPTION
            'Experiment backing-flag ownership is immutable';
    END IF;
    RETURN NEW;
END
$immutable_experiment_flag_ownership$;

CREATE TRIGGER experiments_immutable_flag_ownership
BEFORE UPDATE OF project_id, flag_key ON experiments
FOR EACH ROW EXECUTE FUNCTION reject_experiment_flag_ownership_change();

CREATE TABLE IF NOT EXISTS config_outbox (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('flag_change', 'experiment_change', 'exposure')),
    dedup_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, kind, dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_config_outbox_pending
    ON config_outbox (available_at, id)
    WHERE processed_at IS NULL;
