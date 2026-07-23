-- Migration 040: append-only evidence for operator analytics deletions.
--
-- User targets are represented only by a canonical SHA-256 digest. The audit
-- ledger records a requested event before ClickHouse mutations and a completed
-- event only after every explicit target table verifies zero matching rows.
CREATE TABLE analytics_data_deletion_audit (
    request_id     UUID NOT NULL,
    event_type     TEXT NOT NULL,
    scope          TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    target_sha256  TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    actor          TEXT NOT NULL,
    reason         TEXT NOT NULL,
    details        JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (request_id, event_type),
    CONSTRAINT analytics_data_deletion_event_type_check CHECK (
        event_type IN ('requested', 'completed')
    ),
    CONSTRAINT analytics_data_deletion_scope_check CHECK (
        scope IN ('project', 'user')
    ),
    CONSTRAINT analytics_data_deletion_project_check CHECK (
        project_id ~ '^[A-Za-z0-9]{1,64}$'
    ),
    CONSTRAINT analytics_data_deletion_target_hash_check CHECK (
        target_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT analytics_data_deletion_request_hash_check CHECK (
        request_sha256 ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT analytics_data_deletion_actor_check CHECK (
        char_length(actor) BETWEEN 1 AND 512
        AND btrim(actor) <> ''
    ),
    CONSTRAINT analytics_data_deletion_reason_check CHECK (
        char_length(reason) BETWEEN 1 AND 2000
        AND btrim(reason) <> ''
    ),
    CONSTRAINT analytics_data_deletion_details_check CHECK (
        jsonb_typeof(details) = 'object'
        AND (
            (event_type = 'requested' AND details = '{}'::jsonb)
            OR (
                event_type = 'completed'
                AND details ?& ARRAY[
                    'matched_rows',
                    'anonymous_id_count'
                ]
                AND details - ARRAY[
                    'matched_rows',
                    'anonymous_id_count'
                ] = '{}'::jsonb
                AND jsonb_typeof(details -> 'matched_rows') = 'object'
                AND jsonb_typeof(details -> 'anonymous_id_count') = 'number'
                AND details ->> 'anonymous_id_count' ~ '^(0|[1-9][0-9]*)$'
                AND (details -> 'matched_rows') ?& ARRAY[
                    'events',
                    'experiment_event_deliveries',
                    'feature_flag_exposures',
                    'frontend_health_events',
                    'sessions',
                    'identity_alias_assertions'
                ]
                AND (details -> 'matched_rows') - ARRAY[
                    'events',
                    'experiment_event_deliveries',
                    'feature_flag_exposures',
                    'frontend_health_events',
                    'sessions',
                    'identity_alias_assertions'
                ] = '{}'::jsonb
                AND jsonb_typeof(
                    details #> '{matched_rows,events}'
                ) = 'number'
                AND details #>> '{matched_rows,events}'
                    ~ '^(0|[1-9][0-9]*)$'
                AND jsonb_typeof(
                    details #> '{matched_rows,experiment_event_deliveries}'
                ) = 'number'
                AND details #>> '{matched_rows,experiment_event_deliveries}'
                    ~ '^(0|[1-9][0-9]*)$'
                AND jsonb_typeof(
                    details #> '{matched_rows,feature_flag_exposures}'
                ) = 'number'
                AND details #>> '{matched_rows,feature_flag_exposures}'
                    ~ '^(0|[1-9][0-9]*)$'
                AND jsonb_typeof(
                    details #> '{matched_rows,frontend_health_events}'
                ) = 'number'
                AND details #>> '{matched_rows,frontend_health_events}'
                    ~ '^(0|[1-9][0-9]*)$'
                AND jsonb_typeof(
                    details #> '{matched_rows,sessions}'
                ) = 'number'
                AND details #>> '{matched_rows,sessions}'
                    ~ '^(0|[1-9][0-9]*)$'
                AND jsonb_typeof(
                    details #> '{matched_rows,identity_alias_assertions}'
                ) = 'number'
                AND details #>> '{matched_rows,identity_alias_assertions}'
                    ~ '^(0|[1-9][0-9]*)$'
            )
        )
    )
);

CREATE FUNCTION validate_analytics_data_deletion_audit_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $analytics_data_deletion_audit_insert$
DECLARE
    requested analytics_data_deletion_audit%ROWTYPE;
BEGIN
    NEW.recorded_at := clock_timestamp();

    IF NEW.event_type = 'requested' THEN
        RETURN NEW;
    END IF;

    SELECT *
    INTO requested
    FROM analytics_data_deletion_audit
    WHERE request_id = NEW.request_id
      AND event_type = 'requested';

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'analytics deletion completion requires a requested event';
    END IF;

    IF requested.scope <> NEW.scope
       OR requested.project_id <> NEW.project_id
       OR requested.target_sha256 <> NEW.target_sha256
       OR requested.request_sha256 <> NEW.request_sha256
       OR requested.actor <> NEW.actor
       OR requested.reason <> NEW.reason THEN
        RAISE EXCEPTION
            'analytics deletion completion does not match its request';
    END IF;

    RETURN NEW;
END;
$analytics_data_deletion_audit_insert$;

CREATE TRIGGER analytics_data_deletion_audit_validate_insert
BEFORE INSERT ON analytics_data_deletion_audit
FOR EACH ROW
EXECUTE FUNCTION validate_analytics_data_deletion_audit_insert();

CREATE FUNCTION prevent_analytics_data_deletion_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $analytics_data_deletion_audit_immutable$
BEGIN
    RAISE EXCEPTION
        'analytics data deletion audit records are immutable';
END;
$analytics_data_deletion_audit_immutable$;

CREATE TRIGGER analytics_data_deletion_audit_no_update_delete
BEFORE UPDATE OR DELETE ON analytics_data_deletion_audit
FOR EACH ROW
EXECUTE FUNCTION prevent_analytics_data_deletion_audit_mutation();

CREATE TRIGGER analytics_data_deletion_audit_no_truncate
BEFORE TRUNCATE ON analytics_data_deletion_audit
FOR EACH STATEMENT
EXECUTE FUNCTION prevent_analytics_data_deletion_audit_mutation();

CREATE INDEX idx_analytics_data_deletion_audit_project_time
    ON analytics_data_deletion_audit (
        project_id,
        recorded_at,
        request_id,
        event_type
    );

REVOKE ALL ON analytics_data_deletion_audit FROM PUBLIC;

COMMENT ON TABLE analytics_data_deletion_audit IS
    'Append-only requested/completed evidence for maintenance-fenced analytics deletion';
COMMENT ON COLUMN analytics_data_deletion_audit.target_sha256 IS
    'Canonical SHA-256 target digest; raw user identifiers are never retained here';
