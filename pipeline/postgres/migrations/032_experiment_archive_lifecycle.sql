-- Migration 032: preserve launched experiment authority and lifecycle history.
--
-- Drafts may be physically deleted. Every experiment that has left draft is
-- archived in place, with an immutable tombstone and an independent audit
-- ledger that remains available even when a draft is deleted.

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS archived_by TEXT;

ALTER TABLE experiments
    ADD CONSTRAINT experiments_archive_metadata_check CHECK (
        (archived_at IS NULL AND archived_by IS NULL)
        OR (
            archived_at IS NOT NULL
            AND archived_by IS NOT NULL
            AND btrim(archived_by) <> ''
        )
    );

CREATE TABLE experiment_audit_log (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    experiment_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN (
            'experiment_created',
            'experiment_updated',
            'experiment_status_changed',
            'experiment_archived',
            'experiment_deleted'
        )
    ),
    actor TEXT NOT NULL CHECK (btrim(actor) <> ''),
    previous_version INTEGER CHECK (
        previous_version IS NULL OR previous_version >= 1
    ),
    new_version INTEGER CHECK (new_version IS NULL OR new_version >= 1),
    before JSONB,
    after JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_experiment_audit_project_experiment
    ON experiment_audit_log (
        project_id, experiment_key, created_at DESC, id DESC
    );

CREATE OR REPLACE FUNCTION public.apdl_reject_experiment_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_experiment_audit_mutation$
BEGIN
    RAISE EXCEPTION 'experiment lifecycle audit rows are immutable';
END
$apdl_reject_experiment_audit_mutation$;

CREATE TRIGGER experiment_audit_log_no_update_delete
BEFORE UPDATE OR DELETE ON experiment_audit_log
FOR EACH ROW EXECUTE FUNCTION public.apdl_reject_experiment_audit_mutation();

CREATE TRIGGER experiment_audit_log_no_truncate
BEFORE TRUNCATE ON experiment_audit_log
FOR EACH STATEMENT EXECUTE FUNCTION public.apdl_reject_experiment_audit_mutation();

CREATE OR REPLACE FUNCTION public.apdl_enforce_experiment_archive_lifecycle()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_experiment_archive_lifecycle$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION
                'only draft experiments may be physically deleted';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.archived_at IS NOT NULL THEN
        RAISE EXCEPTION 'archived experiments are immutable';
    END IF;

    IF NEW.archived_at IS NOT NULL THEN
        IF OLD.status = 'draft' THEN
            RAISE EXCEPTION
                'draft experiments must be deleted instead of archived';
        END IF;
        IF NEW.archived_by IS NULL OR btrim(NEW.archived_by) = '' THEN
            RAISE EXCEPTION 'experiment archive actor is required';
        END IF;
        IF (to_jsonb(NEW) - ARRAY[
                'status', 'end_date', 'archived_at', 'archived_by',
                'version', 'updated_at'
            ]) IS DISTINCT FROM (
                to_jsonb(OLD) - ARRAY[
                    'status', 'end_date', 'archived_at', 'archived_by',
                    'version', 'updated_at'
                ]
            )
           OR NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION
                'experiment archive must preserve the launched contract';
        END IF;
        IF OLD.status IN ('scheduled', 'running') THEN
            IF NEW.status <> 'stopped' THEN
                RAISE EXCEPTION
                    'archiving an open experiment must stop it';
            END IF;
            IF OLD.status = 'scheduled' AND NEW.end_date IS NOT NULL THEN
                RAISE EXCEPTION
                    'archived scheduled experiment must have no analysis window';
            END IF;
            IF OLD.status = 'running'
               AND (
                   NEW.end_date IS NULL
                   OR NEW.end_date <= OLD.start_date
                   OR NEW.end_date > OLD.end_date
               ) THEN
                RAISE EXCEPTION
                    'archived running experiment requires a bounded actual end';
            END IF;
        ELSIF NEW.status IS DISTINCT FROM OLD.status
              OR NEW.end_date IS DISTINCT FROM OLD.end_date THEN
            RAISE EXCEPTION
                'archiving a terminal experiment cannot rewrite its lifecycle';
        END IF;
    END IF;
    RETURN NEW;
END
$apdl_enforce_experiment_archive_lifecycle$;

CREATE TRIGGER experiments_enforce_archive_lifecycle
BEFORE UPDATE OR DELETE ON experiments
FOR EACH ROW EXECUTE FUNCTION public.apdl_enforce_experiment_archive_lifecycle();

COMMENT ON COLUMN experiments.archived_at IS
    'Tombstone time for an experiment that left draft; archived rows are immutable';
COMMENT ON TABLE experiment_audit_log IS
    'Append-only experiment lifecycle evidence retained across draft deletion';
