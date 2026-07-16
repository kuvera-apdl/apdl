-- Migration 024: Require segmented evidence for evaluated Codegen publication.
--
-- publication_authorization@2 binds only the aggregate evaluation report. It
-- cannot be upgraded truthfully because it did not record the segmented report
-- used to prove risk, ecosystem, and task-type coverage. Preserve those records
-- for audit, remove them from active authority, and admit only the strict
-- publication_authorization@3 contract or the separate local-development grant.

ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS publication_authorization_segmentless_legacy JSONB;

UPDATE codegen_changesets
SET publication_authorization_segmentless_legacy = publication_authorization,
    publication_authorization = NULL
WHERE publication_authorization IS NOT NULL
  AND publication_authorization->>'schema_version'
      = 'publication_authorization@2';

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_publication_authorization_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_publication_authorization_check
    CHECK ((
        publication_authorization IS NULL
        OR publication_authorization->>'schema_version'
            = 'publication_authorization@3'
        OR (
            publication_authorization->>'schema_version'
                = 'development_publication_authorization@1'
            AND publication_authorization->>'authority' = 'local_development'
            AND publication_authorization->'request'->>'schema_version'
                = 'development_publication_request@1'
            AND publication_authorization->'request'->>'requested_stage'
                = 'development_pr'
            AND publication_authorization->'request'->>'codegen_revision'
                = 'local-development'
            AND publication_authorization->'decision'->>'schema_version'
                = 'development_publication_decision@1'
            AND publication_authorization->'decision'->>'requested_stage'
                = 'development_pr'
            AND publication_authorization->'draft_only' = 'true'::jsonb
        )
    ) IS TRUE);

COMMENT ON COLUMN codegen_changesets.publication_authorization_segmentless_legacy IS
    'Audit-only evaluated publication_authorization@2 JSON without segmented evidence.';
COMMENT ON COLUMN codegen_changesets.publication_authorization IS
    'Strict evaluated publication_authorization@3 or draft-only local development_publication_authorization@1.';
