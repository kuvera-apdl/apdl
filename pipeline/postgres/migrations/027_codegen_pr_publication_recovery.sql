-- Migration 027: Durable, idempotent Codegen pull-request publication.
--
-- GitHub can accept a PR before the controller validates its response. Keep a
-- complete pre-mutation intent and every accepted external identity in an
-- append-only journal so a restart searches the deterministic APDL branch
-- before retrying POST /pulls.

CREATE TABLE IF NOT EXISTS codegen_pull_request_publication_events (
    event_id TEXT PRIMARY KEY,
    event_sequence BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,
    changeset_id TEXT NOT NULL
        REFERENCES codegen_changesets(changeset_id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    intent_event_id TEXT,
    cleanup_request_event_id TEXT,
    pr_number INTEGER,
    github_url TEXT,
    recorded_at TIMESTAMPTZ NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL,
    CONSTRAINT codegen_pr_publication_event_id_check
        CHECK (event_id ~ '^cpub_[0-9a-f]{32}$'),
    CONSTRAINT codegen_pr_publication_event_type_check
        CHECK (event_type IN (
            'intent_recorded',
            'branch_published',
            'create_accepted',
            'identity_validated',
            'cleanup_requested',
            'cleanup_confirmed',
            'manual_intervention',
            'recovery_deferred'
        )),
    CONSTRAINT codegen_pr_publication_intent_link_check
        CHECK (
            (event_type = 'intent_recorded' AND intent_event_id IS NULL)
            OR
            (event_type <> 'intent_recorded'
                AND intent_event_id IS NOT NULL
                AND intent_event_id ~ '^cpub_[0-9a-f]{32}$')
        ),
    CONSTRAINT codegen_pr_publication_cleanup_link_check
        CHECK (
            (
                event_type = 'cleanup_confirmed'
                AND cleanup_request_event_id IS NOT NULL
                AND cleanup_request_event_id ~ '^cpub_[0-9a-f]{32}$'
            )
            OR
            (
                event_type <> 'cleanup_confirmed'
                AND cleanup_request_event_id IS NULL
            )
        ),
    CONSTRAINT codegen_pr_publication_pr_number_check
        CHECK (pr_number IS NULL OR pr_number > 0),
    CONSTRAINT codegen_pr_publication_payload_object_check
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT codegen_pr_publication_payload_identity_check
        CHECK (
            payload ?& ARRAY[
                'schema_version', 'event_id', 'changeset_id',
                'recorded_at', 'event_type'
            ]::text[]
            AND payload->>'event_id' IS NOT DISTINCT FROM event_id
            AND payload->>'changeset_id' IS NOT DISTINCT FROM changeset_id
            AND payload->>'event_type' IS NOT DISTINCT FROM event_type
            AND (
                (event_type = 'intent_recorded'
                    AND NOT (payload ? 'intent_event_id'))
                OR
                (event_type <> 'intent_recorded'
                    AND payload ? 'intent_event_id'
                    AND payload->>'intent_event_id'
                        IS NOT DISTINCT FROM intent_event_id)
            )
            AND (
                (event_type = 'cleanup_confirmed'
                    AND payload ? 'cleanup_request_event_id'
                    AND payload->>'cleanup_request_event_id'
                        IS NOT DISTINCT FROM cleanup_request_event_id)
                OR
                (event_type <> 'cleanup_confirmed'
                    AND NOT (payload ? 'cleanup_request_event_id'))
            )
        ),
    CONSTRAINT codegen_pr_publication_payload_type_check
        CHECK (
            (event_type = 'intent_recorded'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_publication_intent@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'branch_published'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_branch_published@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'create_accepted'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_create_accepted@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'identity_validated'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_identity_validated@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'cleanup_requested'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_cleanup_requested@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'cleanup_confirmed'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_cleanup_confirmed@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'manual_intervention'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_manual_intervention@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
            OR
            (event_type = 'recovery_deferred'
                AND payload->>'schema_version'
                    IS NOT DISTINCT FROM 'pull_request_recovery_deferred@1'
                AND payload->>'event_type' IS NOT DISTINCT FROM event_type)
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_codegen_pr_publication_intent
    ON codegen_pull_request_publication_events (changeset_id)
    WHERE event_type = 'intent_recorded';

CREATE INDEX IF NOT EXISTS idx_codegen_pr_publication_recovery
    ON codegen_pull_request_publication_events
       (changeset_id, event_sequence DESC);

CREATE OR REPLACE FUNCTION enforce_codegen_pr_publication_intent_link()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.event_type <> 'intent_recorded'
       AND NOT EXISTS (
           SELECT 1
           FROM codegen_pull_request_publication_events AS intent
           WHERE intent.event_id = NEW.intent_event_id
             AND intent.changeset_id = NEW.changeset_id
             AND intent.event_type = 'intent_recorded'
       ) THEN
        RAISE EXCEPTION
            'codegen PR publication event requires its same-changeset intent';
    END IF;
    IF NEW.event_type = 'cleanup_confirmed'
       AND NOT EXISTS (
           SELECT 1
           FROM codegen_pull_request_publication_events AS request
           WHERE request.event_id = NEW.cleanup_request_event_id
             AND request.changeset_id = NEW.changeset_id
             AND request.intent_event_id = NEW.intent_event_id
             AND request.event_type = 'cleanup_requested'
             AND request.pr_number IS NOT DISTINCT FROM NEW.pr_number
             AND request.github_url IS NOT DISTINCT FROM NEW.github_url
             AND request.payload->>'next_action'
                 IS NOT DISTINCT FROM NEW.payload->>'next_action'
             AND request.payload->>'reason'
                 IS NOT DISTINCT FROM NEW.payload->>'reason'
       ) THEN
        RAISE EXCEPTION
            'codegen PR cleanup confirmation requires its exact request';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS codegen_pr_publication_events_require_intent
    ON codegen_pull_request_publication_events;
CREATE TRIGGER codegen_pr_publication_events_require_intent
BEFORE INSERT ON codegen_pull_request_publication_events
FOR EACH ROW
EXECUTE FUNCTION enforce_codegen_pr_publication_intent_link();

CREATE OR REPLACE FUNCTION reject_codegen_pr_publication_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'codegen pull-request publication events are append-only';
END;
$$;

DROP TRIGGER IF EXISTS codegen_pr_publication_events_append_only
    ON codegen_pull_request_publication_events;
CREATE TRIGGER codegen_pr_publication_events_append_only
BEFORE UPDATE OR DELETE ON codegen_pull_request_publication_events
FOR EACH ROW
EXECUTE FUNCTION reject_codegen_pr_publication_event_mutation();

COMMENT ON TABLE codegen_pull_request_publication_events IS
    'Append-only intent, accepted GitHub identity, validation, cleanup, and recovery journal.';
