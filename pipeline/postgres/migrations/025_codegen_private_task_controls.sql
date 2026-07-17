-- Migration 025: Separate public Codegen task data from private execution controls.
--
-- Task context is supplied by agents:manage callers and may contain useful JSON
-- data, but it must never select review strictness or invoke a mechanical revert.
-- Existing context controls are untrusted, so remove rather than backfill them.

DO $validate_codegen_task_context$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM codegen_changesets
        WHERE jsonb_typeof(task) IS DISTINCT FROM 'object'
           OR (
                task ? 'context'
                AND jsonb_typeof(task->'context') IS DISTINCT FROM 'object'
           )
    ) THEN
        RAISE EXCEPTION
            'codegen_changesets.task must contain an object-shaped context before migration 025';
    END IF;
END
$validate_codegen_task_context$;

UPDATE codegen_changesets
SET task = jsonb_set(
    task,
    '{context}',
    COALESCE(task->'context', '{}'::jsonb)
        - ARRAY[
            'revert_sha',
            'reverts_changeset',
            'reverts_pr_number',
            'retry_of',
            'risk_level'
        ]::text[],
    true
);

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_public_task_context_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_public_task_context_check
    CHECK ((
        jsonb_typeof(task) = 'object'
        AND jsonb_typeof(task->'context') = 'object'
        AND NOT (
            (task->'context') ?| ARRAY[
                'revert_sha',
                'reverts_changeset',
                'reverts_pr_number',
                'retry_of',
                'risk_level'
            ]::text[]
        )
    ) IS TRUE);

ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS control_metadata JSONB NOT NULL DEFAULT
        '{
            "schema_version": "changeset_controls@1",
            "risk_level": "high",
            "revert": null
        }'::jsonb;

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_control_metadata_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_control_metadata_check
    CHECK ((
        jsonb_typeof(control_metadata) = 'object'
        AND control_metadata ?& ARRAY[
            'schema_version',
            'risk_level',
            'revert'
        ]::text[]
        AND (
            control_metadata - ARRAY[
                'schema_version',
                'risk_level',
                'revert'
            ]::text[]
        ) = '{}'::jsonb
        AND control_metadata->>'schema_version' = 'changeset_controls@1'
        AND control_metadata->>'risk_level' IN ('low', 'medium', 'high')
        AND (
            control_metadata->'revert' = 'null'::jsonb
            OR (
                jsonb_typeof(control_metadata->'revert') = 'object'
                AND (control_metadata->'revert') ?& ARRAY[
                    'source_changeset_id',
                    'merge_sha'
                ]::text[]
                AND (
                    (control_metadata->'revert') - ARRAY[
                        'source_changeset_id',
                        'merge_sha'
                    ]::text[]
                ) = '{}'::jsonb
                AND jsonb_typeof(
                    control_metadata->'revert'->'source_changeset_id'
                ) = 'string'
                AND char_length(
                    control_metadata->'revert'->>'source_changeset_id'
                ) BETWEEN 1 AND 128
                AND (
                    control_metadata->'revert'->'merge_sha' = 'null'::jsonb
                    OR (
                        jsonb_typeof(
                            control_metadata->'revert'->'merge_sha'
                        ) = 'string'
                        AND char_length(
                            control_metadata->'revert'->>'merge_sha'
                        ) BETWEEN 1 AND 128
                    )
                )
            )
        )
    ) IS TRUE);

CREATE OR REPLACE FUNCTION validate_codegen_changeset_private_controls()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    revert_control JSONB;
    source_project_id TEXT;
    source_status TEXT;
    source_merge_sha TEXT;
    target_merge_sha TEXT;
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.control_metadata IS DISTINCT FROM OLD.control_metadata THEN
        RAISE EXCEPTION 'codegen changeset control metadata is immutable';
    END IF;

    revert_control := NEW.control_metadata->'revert';
    IF revert_control IS NULL OR revert_control = 'null'::jsonb THEN
        RETURN NEW;
    END IF;

    SELECT project_id, status, merge_sha
    INTO source_project_id, source_status, source_merge_sha
    FROM codegen_changesets
    WHERE changeset_id = revert_control->>'source_changeset_id';

    IF NOT FOUND
       OR source_project_id IS DISTINCT FROM NEW.project_id
       OR source_status IS DISTINCT FROM 'merged' THEN
        RAISE EXCEPTION
            'private revert control requires a merged source in the same project';
    END IF;

    target_merge_sha := revert_control->>'merge_sha';
    IF source_merge_sha IS DISTINCT FROM target_merge_sha THEN
        RAISE EXCEPTION
            'private revert target must equal the recorded source merge SHA';
    END IF;

    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS codegen_changesets_private_controls_trigger
    ON codegen_changesets;
CREATE TRIGGER codegen_changesets_private_controls_trigger
BEFORE INSERT OR UPDATE OF control_metadata, project_id
ON codegen_changesets
FOR EACH ROW
EXECUTE FUNCTION validate_codegen_changeset_private_controls();

COMMENT ON COLUMN codegen_changesets.control_metadata IS
    'Immutable changeset_controls@1 execution authority; never returned as public task data.';
