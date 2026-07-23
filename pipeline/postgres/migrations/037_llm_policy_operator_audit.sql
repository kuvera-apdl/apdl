-- Durable operator evidence for every project LLM policy replacement.
--
-- Snapshots deliberately store only a SHA-256 digest of each endpoint.  This
-- preserves exact-policy evidence without persisting credentials that may have
-- been embedded in a legacy endpoint URL.
CREATE TABLE llm_project_policy_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_policy JSONB NOT NULL,
    next_policy JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT llm_project_policy_audit_actor_check CHECK (
        char_length(actor) BETWEEN 1 AND 512
        AND actor = btrim(actor)
        AND position(chr(10) IN actor) = 0
        AND position(chr(13) IN actor) = 0
    ),
    CONSTRAINT llm_project_policy_audit_reason_check CHECK (
        char_length(reason) BETWEEN 1 AND 2000
        AND reason = btrim(reason)
        AND position(chr(10) IN reason) = 0
        AND position(chr(13) IN reason) = 0
    ),
    CONSTRAINT llm_project_policy_audit_previous_check CHECK (
        jsonb_typeof(previous_policy) = 'object'
        AND previous_policy ->> 'schema' = 'llm_project_policy_snapshot@1'
        AND jsonb_typeof(previous_policy -> 'project_policy') = 'object'
        AND jsonb_typeof(previous_policy -> 'provider_policies') = 'array'
    ),
    CONSTRAINT llm_project_policy_audit_next_check CHECK (
        jsonb_typeof(next_policy) = 'object'
        AND next_policy ->> 'schema' = 'llm_project_policy_snapshot@1'
        AND jsonb_typeof(next_policy -> 'project_policy') = 'object'
        AND jsonb_typeof(next_policy -> 'provider_policies') = 'array'
    )
);

CREATE INDEX llm_project_policy_audit_project_created_idx
    ON llm_project_policy_audit (project_id, created_at DESC, audit_id DESC);

CREATE OR REPLACE FUNCTION apdl_reject_llm_policy_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_llm_policy_audit_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'LLM project policy audit rows are immutable';
END
$apdl_reject_llm_policy_audit_mutation$;

CREATE TRIGGER llm_project_policy_audit_no_update_delete
BEFORE UPDATE OR DELETE ON llm_project_policy_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_llm_policy_audit_mutation();

CREATE TRIGGER llm_project_policy_audit_no_truncate
BEFORE TRUNCATE ON llm_project_policy_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_llm_policy_audit_mutation();
