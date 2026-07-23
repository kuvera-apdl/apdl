-- Migration 041: fair, bounded publication of experiment analysis markers.
--
-- A marker's stream identity remains immutable. Mutable publication state is
-- a strict monotone state machine: pending failures advance a bounded attempt
-- counter and server-time retry deadline; success and quarantine are terminal.
ALTER TABLE experiment_analysis_boundaries
    ADD COLUMN marker_publish_state TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN marker_publish_attempts SMALLINT NOT NULL DEFAULT 0,
    ADD COLUMN marker_publish_next_attempt_at TIMESTAMPTZ DEFAULT now(),
    ADD COLUMN marker_publish_failure_code TEXT,
    ADD COLUMN marker_publish_last_error_at TIMESTAMPTZ,
    ADD COLUMN marker_publish_quarantined_at TIMESTAMPTZ,
    ADD COLUMN marker_publish_observed_stream_id TEXT;

UPDATE experiment_analysis_boundaries
SET marker_publish_state = 'published',
    marker_publish_next_attempt_at = NULL,
    marker_publish_observed_stream_id = marker_stream_id
WHERE marker_stream_id IS NOT NULL;

ALTER TABLE experiment_analysis_boundaries
    ADD CONSTRAINT experiment_analysis_boundaries_publish_attempts_check CHECK (
        marker_publish_attempts BETWEEN 0 AND 5
    ),
    ADD CONSTRAINT experiment_analysis_boundaries_publish_failure_check CHECK (
        marker_publish_failure_code IS NULL
        OR marker_publish_failure_code IN (
            'event_stream_capacity',
            'redis_publish_failed',
            'invalid_redis_marker_id',
            'boundary_authority_update_failed',
            'boundary_authority_update_invalid',
            'invalid_boundary_marker_dedup',
            'invalid_stream_authority',
            'invalid_marker_token',
            'unexpected_publish_failure'
        )
    ),
    ADD CONSTRAINT
        experiment_analysis_boundaries_publish_observed_id_check CHECK (
        marker_publish_observed_stream_id IS NULL
        OR (
            marker_publish_observed_stream_id
                ~ '^[1-9][0-9]*-(0|[1-9][0-9]*)$'
            AND split_part(
                marker_publish_observed_stream_id,
                '-',
                1
            )::numeric <= 18446744073709551615
            AND split_part(
                marker_publish_observed_stream_id,
                '-',
                2
            )::numeric <= 18446744073709551615
        )
    ),
    ADD CONSTRAINT experiment_analysis_boundaries_publish_history_check CHECK (
        (
            marker_publish_attempts = 0
            AND marker_publish_failure_code IS NULL
            AND marker_publish_last_error_at IS NULL
        )
        OR (
            marker_publish_attempts > 0
            AND marker_publish_failure_code IS NOT NULL
            AND marker_publish_last_error_at IS NOT NULL
            AND marker_publish_last_error_at >= requested_at
        )
    ),
    ADD CONSTRAINT experiment_analysis_boundaries_publish_state_check CHECK (
        (
            marker_publish_state = 'pending'
            AND marker_stream_id IS NULL
            AND marked_at IS NULL
            AND marker_publish_attempts < 5
            AND marker_publish_next_attempt_at IS NOT NULL
            AND marker_publish_quarantined_at IS NULL
            AND (
                marker_publish_attempts > 0
                OR marker_publish_observed_stream_id IS NULL
            )
            AND (
                marker_publish_attempts = 0
                OR (
                    marker_publish_failure_code IN (
                        'event_stream_capacity',
                        'redis_publish_failed',
                        'boundary_authority_update_failed',
                        'unexpected_publish_failure'
                    )
                    AND marker_publish_next_attempt_at
                        > marker_publish_last_error_at
                )
            )
        )
        OR (
            marker_publish_state = 'published'
            AND marker_stream_id IS NOT NULL
            AND marked_at IS NOT NULL
            AND marker_publish_next_attempt_at IS NULL
            AND marker_publish_quarantined_at IS NULL
            AND marker_publish_observed_stream_id = marker_stream_id
            AND (
                marker_publish_attempts = 0
                OR marker_publish_failure_code IN (
                    'event_stream_capacity',
                    'redis_publish_failed',
                    'boundary_authority_update_failed',
                    'unexpected_publish_failure'
                )
            )
        )
        OR (
            marker_publish_state = 'quarantined'
            AND marker_stream_id IS NULL
            AND marked_at IS NULL
            AND marker_publish_attempts > 0
            AND marker_publish_next_attempt_at IS NULL
            AND marker_publish_quarantined_at IS NOT NULL
            AND marker_publish_quarantined_at
                >= marker_publish_last_error_at
            AND (
                (
                    marker_publish_attempts = 5
                    AND marker_publish_failure_code IN (
                        'event_stream_capacity',
                        'redis_publish_failed',
                        'boundary_authority_update_failed',
                        'unexpected_publish_failure'
                    )
                )
                OR marker_publish_failure_code IN (
                    'invalid_redis_marker_id',
                    'boundary_authority_update_invalid',
                    'invalid_boundary_marker_dedup',
                    'invalid_stream_authority',
                    'invalid_marker_token'
                )
            )
        )
    ),
    ADD CONSTRAINT
        experiment_analysis_boundaries_observed_stream_identity UNIQUE (
        project_id,
        marker_publish_observed_stream_id
    );

DROP INDEX idx_experiment_analysis_boundaries_unmarked;
CREATE INDEX idx_experiment_analysis_boundaries_publish_due
    ON experiment_analysis_boundaries (
        marker_publish_next_attempt_at,
        requested_at,
        project_id,
        experiment_key,
        config_version
    )
    WHERE marker_publish_state = 'pending'
      AND marker_stream_id IS NULL;

CREATE OR REPLACE FUNCTION enforce_experiment_analysis_boundary_immutability()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $experiment_analysis_boundary_immutable$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'experiment analysis boundaries cannot be deleted';
    END IF;

    IF NEW.project_id <> OLD.project_id
       OR NEW.experiment_key <> OLD.experiment_key
       OR NEW.config_version <> OLD.config_version
       OR NEW.stream_key <> OLD.stream_key
       OR NEW.window_start <> OLD.window_start
       OR NEW.window_end <> OLD.window_end
       OR NEW.marker_token <> OLD.marker_token
       OR NEW.requested_at <> OLD.requested_at
       OR (
           OLD.marker_publish_observed_stream_id IS NOT NULL
           AND NEW.marker_publish_observed_stream_id
               IS DISTINCT FROM OLD.marker_publish_observed_stream_id
       )
    THEN
        RAISE EXCEPTION 'experiment analysis boundary identity is immutable';
    END IF;

    IF OLD.marker_publish_state IN ('published', 'quarantined') THEN
        RAISE EXCEPTION 'experiment analysis boundary publication is terminal';
    END IF;

    IF OLD.marker_publish_state <> 'pending' THEN
        RAISE EXCEPTION 'experiment analysis boundary publication state is invalid';
    END IF;

    IF NEW.marker_publish_state = 'pending' THEN
        IF NEW.marker_publish_attempts
            <> OLD.marker_publish_attempts + 1
        THEN
            RAISE EXCEPTION 'boundary marker retry attempt must advance once';
        END IF;
    ELSIF NEW.marker_publish_state = 'published' THEN
        IF NEW.marker_publish_attempts <> OLD.marker_publish_attempts THEN
            RAISE EXCEPTION 'boundary marker success cannot change attempts';
        END IF;
    ELSIF NEW.marker_publish_state = 'quarantined' THEN
        IF NEW.marker_publish_attempts
            <> OLD.marker_publish_attempts + 1
        THEN
            RAISE EXCEPTION 'boundary marker quarantine must advance once';
        END IF;
    ELSE
        RAISE EXCEPTION 'experiment analysis boundary publication state is invalid';
    END IF;

    RETURN NEW;
END;
$experiment_analysis_boundary_immutable$;

COMMENT ON COLUMN
    experiment_analysis_boundaries.marker_publish_state IS
    'Monotone pending, published, or quarantined marker publication state';
COMMENT ON COLUMN
    experiment_analysis_boundaries.marker_publish_failure_code IS
    'Bounded safe code for the most recent publication failure';
COMMENT ON COLUMN
    experiment_analysis_boundaries.marker_publish_observed_stream_id IS
    'First validated Redis marker ID, retained across retry or quarantine';
