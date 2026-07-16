-- Migration 026: Bind evaluated Codegen publication authority to egress policy.
--
-- publication_authorization@3 proves segmented evaluation coverage, but it does
-- not identify the worker egress policy used during evaluation. Those records
-- cannot be upgraded truthfully. Preserve them for audit, remove them from
-- active authority, and admit only publication_authorization@4 requests whose
-- canonical publication_request@3 egress digest matches the deployment digest.

ALTER TABLE codegen_changesets
    ADD COLUMN IF NOT EXISTS
        publication_authorization_egress_unattested_legacy JSONB;

UPDATE codegen_changesets
SET publication_authorization_egress_unattested_legacy
        = publication_authorization,
    publication_authorization = NULL
WHERE publication_authorization IS NOT NULL
  AND publication_authorization->>'schema_version'
      = 'publication_authorization@3';

ALTER TABLE codegen_changesets
    DROP CONSTRAINT IF EXISTS codegen_changesets_publication_authorization_check;
ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_publication_authorization_check
    CHECK ((
        publication_authorization IS NULL
        OR (
            jsonb_typeof(publication_authorization) = 'object'
            AND publication_authorization ?& ARRAY[
                'schema_version',
                'request',
                'expected_model',
                'expected_codegen_revision',
                'expected_candidate_identity_sha256',
                'expected_egress_policy_sha256',
                'report_sha256',
                'segmented_report_sha256',
                'bundle_sha256',
                'policy_sha256',
                'decision',
                'authorization_sha256'
            ]::text[]
            AND (
                publication_authorization - ARRAY[
                    'schema_version',
                    'request',
                    'expected_model',
                    'expected_codegen_revision',
                    'expected_candidate_identity_sha256',
                    'expected_egress_policy_sha256',
                    'report_sha256',
                    'segmented_report_sha256',
                    'bundle_sha256',
                    'policy_sha256',
                    'decision',
                    'authorization_sha256'
                ]::text[]
            ) = '{}'::jsonb
            AND publication_authorization->>'schema_version'
                = 'publication_authorization@4'
            AND jsonb_typeof(publication_authorization->'request') = 'object'
            AND (publication_authorization->'request') ?& ARRAY[
                'schema_version',
                'requested_stage',
                'risk',
                'model',
                'codegen_revision',
                'candidate_identity_sha256',
                'egress_policy_sha256',
                'canary_identity'
            ]::text[]
            AND (
                (publication_authorization->'request') - ARRAY[
                    'schema_version',
                    'requested_stage',
                    'risk',
                    'model',
                    'codegen_revision',
                    'candidate_identity_sha256',
                    'egress_policy_sha256',
                    'canary_identity'
                ]::text[]
            ) = '{}'::jsonb
            AND publication_authorization->'request'->>'schema_version'
                = 'publication_request@3'
            AND jsonb_typeof(
                publication_authorization->'request'->'egress_policy_sha256'
            ) = 'string'
            AND publication_authorization->'request'->>'egress_policy_sha256'
                ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(
                publication_authorization->'expected_egress_policy_sha256'
            ) = 'string'
            AND publication_authorization->>'expected_egress_policy_sha256'
                ~ '^[0-9a-f]{64}$'
            AND publication_authorization->'request'->>'egress_policy_sha256'
                = publication_authorization->>'expected_egress_policy_sha256'
        )
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

COMMENT ON COLUMN
    codegen_changesets.publication_authorization_egress_unattested_legacy IS
    'Audit-only publication_authorization@3 JSON without egress-policy evidence.';
COMMENT ON COLUMN codegen_changesets.publication_authorization IS
    'Strict egress-bound publication_authorization@4 or draft-only local development_publication_authorization@1.';
