-- Migration 011: Admit the explicit local-development Codegen authorization.
--
-- Evaluated publication continues to use publication_authorization@2. Local
-- `make dev-all` runs use a separate, draft-only authorization contract so a
-- development grant can never be confused with rollout evidence.

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_publication_authorization_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_publication_authorization_check
    CHECK ((
        publication_authorization IS NULL
        OR publication_authorization->>'schema_version'
            = 'publication_authorization@2'
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

COMMENT ON COLUMN codegen_changesets.publication_authorization IS
    'Strict evaluated publication_authorization@2 or draft-only local development_publication_authorization@1.';
