-- Migration 010: Make Codegen publication authority candidate-identity bound.
--
-- publication_authorization@1 proves only model + free-form revision. It cannot
-- be upgraded truthfully because the exact controller image, candidate image,
-- and normalized behavior digest were never recorded. Preserve that JSON for
-- audit, remove it from the active authorization column, and admit only the
-- strict publication_authorization@2 contract from this point forward.

ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS publication_authorization_legacy JSONB;

UPDATE codegen_changesets
SET publication_authorization_legacy = publication_authorization,
    publication_authorization = NULL
WHERE publication_authorization IS NOT NULL
  AND publication_authorization->>'schema_version'
      IS DISTINCT FROM 'publication_authorization@2';

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_publication_authorization_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_publication_authorization_check
    CHECK (
        publication_authorization IS NULL
        OR (
            publication_authorization->>'schema_version'
                = 'publication_authorization@2'
        ) IS TRUE
    );

COMMENT ON COLUMN codegen_changesets.publication_authorization_legacy IS
    'Audit-only pre-v2 publication JSON; never active publication authority.';
