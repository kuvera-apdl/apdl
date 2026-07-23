-- Migration 038: authoritative event-pipeline completeness and frozen analyses.

CREATE TABLE event_pipeline_watermarks (
    project_id TEXT PRIMARY KEY,
    stream_key TEXT NOT NULL UNIQUE,
    provenance_start_stream_id TEXT NOT NULL,
    contiguous_stream_id TEXT NOT NULL,
    consumer_group_entries_read BIGINT NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT event_pipeline_watermarks_project_check CHECK (
        project_id ~ '^[A-Za-z0-9]{1,64}$'
    ),
    CONSTRAINT event_pipeline_watermarks_stream_check CHECK (
        stream_key = 'events:raw:' || project_id
    ),
    CONSTRAINT event_pipeline_watermarks_start_id_check CHECK (
        provenance_start_stream_id ~ '^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$'
        AND split_part(provenance_start_stream_id, '-', 1)::numeric
            <= 18446744073709551615
        AND split_part(provenance_start_stream_id, '-', 2)::numeric
            <= 18446744073709551615
    ),
    CONSTRAINT event_pipeline_watermarks_frontier_id_check CHECK (
        contiguous_stream_id ~ '^(0|[1-9][0-9]*)-(0|[1-9][0-9]*)$'
        AND split_part(contiguous_stream_id, '-', 1)::numeric
            <= 18446744073709551615
        AND split_part(contiguous_stream_id, '-', 2)::numeric
            <= 18446744073709551615
    ),
    CONSTRAINT event_pipeline_watermarks_range_check CHECK (
        split_part(provenance_start_stream_id, '-', 1)::numeric
            < split_part(contiguous_stream_id, '-', 1)::numeric
        OR (
            split_part(provenance_start_stream_id, '-', 1)::numeric
                = split_part(contiguous_stream_id, '-', 1)::numeric
            AND split_part(provenance_start_stream_id, '-', 2)::numeric
                <= split_part(contiguous_stream_id, '-', 2)::numeric
        )
    ),
    CONSTRAINT event_pipeline_watermarks_entries_read_check CHECK (
        consumer_group_entries_read >= 0
    ),
    CONSTRAINT event_pipeline_watermarks_status_check CHECK (
        (status = 'healthy' AND failure_reason IS NULL)
        OR (
            status = 'degraded'
            AND failure_reason IN (
                'legacy_state_unverifiable',
                'dead_lettered_event',
                'lost_pending_entry',
                'stream_state_unverifiable'
            )
        )
    )
);

CREATE TABLE experiment_analysis_boundaries (
    project_id TEXT NOT NULL,
    experiment_key TEXT NOT NULL,
    config_version BIGINT NOT NULL,
    stream_key TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    marker_token TEXT NOT NULL UNIQUE,
    marker_stream_id TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    marked_at TIMESTAMPTZ,
    PRIMARY KEY (project_id, experiment_key, config_version),
    CONSTRAINT experiment_analysis_boundaries_marker_identity UNIQUE (
        project_id,
        experiment_key,
        config_version,
        marker_stream_id
    ),
    CONSTRAINT experiment_analysis_boundaries_project_check CHECK (
        project_id ~ '^[A-Za-z0-9]{1,64}$'
    ),
    CONSTRAINT experiment_analysis_boundaries_key_check CHECK (
        experiment_key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
    ),
    CONSTRAINT experiment_analysis_boundaries_version_check CHECK (
        config_version > 0
    ),
    CONSTRAINT experiment_analysis_boundaries_stream_check CHECK (
        stream_key = 'events:raw:' || project_id
    ),
    CONSTRAINT experiment_analysis_boundaries_window_check CHECK (
        window_end > window_start
    ),
    CONSTRAINT experiment_analysis_boundaries_token_check CHECK (
        marker_token ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT experiment_analysis_boundaries_marker_check CHECK (
        (marker_stream_id IS NULL AND marked_at IS NULL)
        OR (
            marker_stream_id ~ '^[1-9][0-9]*-(0|[1-9][0-9]*)$'
            AND split_part(marker_stream_id, '-', 1)::numeric
                <= 18446744073709551615
            AND split_part(marker_stream_id, '-', 2)::numeric
                <= 18446744073709551615
            AND marked_at IS NOT NULL
        )
    )
);

CREATE INDEX idx_experiment_analysis_boundaries_unmarked
    ON experiment_analysis_boundaries (requested_at, project_id)
    WHERE marker_stream_id IS NULL;

CREATE TABLE experiment_analysis_snapshots (
    project_id TEXT NOT NULL,
    experiment_key TEXT NOT NULL,
    config_version BIGINT NOT NULL,
    boundary_stream_id TEXT NOT NULL,
    snapshot_payload JSONB NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, experiment_key, config_version),
    FOREIGN KEY (
        project_id,
        experiment_key,
        config_version,
        boundary_stream_id
    )
        REFERENCES experiment_analysis_boundaries (
            project_id,
            experiment_key,
            config_version,
            marker_stream_id
        ),
    CONSTRAINT experiment_analysis_snapshots_boundary_check CHECK (
        boundary_stream_id ~ '^[1-9][0-9]*-(0|[1-9][0-9]*)$'
        AND split_part(boundary_stream_id, '-', 1)::numeric
            <= 18446744073709551615
        AND split_part(boundary_stream_id, '-', 2)::numeric
            <= 18446744073709551615
    ),
    CONSTRAINT experiment_analysis_snapshots_payload_check CHECK (
        jsonb_typeof(snapshot_payload) = 'object'
        AND snapshot_payload ->> 'analysis_status' = 'decision_snapshot'
        AND snapshot_payload ->> 'data_completeness' = 'verified'
        AND snapshot_payload ->> 'experiment_key' = experiment_key
        AND (snapshot_payload ->> 'config_version')::BIGINT = config_version
    ),
    CONSTRAINT experiment_analysis_snapshots_sha256_check CHECK (
        snapshot_sha256 ~ '^[0-9a-f]{64}$'
    )
);

CREATE FUNCTION enforce_event_pipeline_watermark_monotonicity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'event pipeline watermarks cannot be deleted';
    END IF;

    IF NEW.project_id <> OLD.project_id
       OR NEW.stream_key <> OLD.stream_key
       OR NEW.provenance_start_stream_id <> OLD.provenance_start_stream_id
    THEN
        RAISE EXCEPTION 'event pipeline watermark identity is immutable';
    END IF;

    IF split_part(NEW.contiguous_stream_id, '-', 1)::numeric
           < split_part(OLD.contiguous_stream_id, '-', 1)::numeric
       OR (
           split_part(NEW.contiguous_stream_id, '-', 1)::numeric
               = split_part(OLD.contiguous_stream_id, '-', 1)::numeric
           AND split_part(NEW.contiguous_stream_id, '-', 2)::numeric
               < split_part(OLD.contiguous_stream_id, '-', 2)::numeric
       )
    THEN
        RAISE EXCEPTION 'event pipeline watermark cannot move backwards';
    END IF;

    IF NEW.consumer_group_entries_read < OLD.consumer_group_entries_read THEN
        RAISE EXCEPTION 'consumer group delivery count cannot move backwards';
    END IF;

    IF OLD.status = 'degraded'
       AND (
           NEW.status <> OLD.status
           OR NEW.failure_reason IS DISTINCT FROM OLD.failure_reason
       )
    THEN
        RAISE EXCEPTION 'event pipeline degradation is irreversible';
    END IF;

    IF NEW.updated_at < OLD.updated_at THEN
        RAISE EXCEPTION 'event pipeline watermark timestamp cannot move backwards';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER event_pipeline_watermarks_monotonic
BEFORE UPDATE OR DELETE ON event_pipeline_watermarks
FOR EACH ROW
EXECUTE FUNCTION enforce_event_pipeline_watermark_monotonicity();

CREATE FUNCTION enforce_experiment_analysis_boundary_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
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
       OR (OLD.marker_stream_id IS NOT NULL AND NEW.marker_stream_id IS DISTINCT FROM OLD.marker_stream_id)
       OR (OLD.marked_at IS NOT NULL AND NEW.marked_at IS DISTINCT FROM OLD.marked_at)
       OR (OLD.marker_stream_id IS NULL AND NEW.marker_stream_id IS NULL AND NEW.marked_at IS NOT NULL)
       OR (OLD.marker_stream_id IS NULL AND NEW.marker_stream_id IS NOT NULL AND NEW.marked_at IS NULL)
    THEN
        RAISE EXCEPTION 'experiment analysis boundary identity is immutable';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER experiment_analysis_boundaries_immutable
BEFORE UPDATE OR DELETE ON experiment_analysis_boundaries
FOR EACH ROW
EXECUTE FUNCTION enforce_experiment_analysis_boundary_immutability();

CREATE FUNCTION reject_experiment_analysis_snapshot_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'experiment analysis snapshots are immutable';
END;
$$;

CREATE TRIGGER experiment_analysis_snapshots_immutable
BEFORE UPDATE OR DELETE ON experiment_analysis_snapshots
FOR EACH ROW
EXECUTE FUNCTION reject_experiment_analysis_snapshot_mutation();

CREATE FUNCTION reject_experiment_completeness_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'experiment completeness authorities cannot be truncated';
END;
$$;

CREATE TRIGGER event_pipeline_watermarks_no_truncate
BEFORE TRUNCATE ON event_pipeline_watermarks
FOR EACH STATEMENT
EXECUTE FUNCTION reject_experiment_completeness_truncate();

CREATE TRIGGER experiment_analysis_boundaries_no_truncate
BEFORE TRUNCATE ON experiment_analysis_boundaries
FOR EACH STATEMENT
EXECUTE FUNCTION reject_experiment_completeness_truncate();

CREATE TRIGGER experiment_analysis_snapshots_no_truncate
BEFORE TRUNCATE ON experiment_analysis_snapshots
FOR EACH STATEMENT
EXECUTE FUNCTION reject_experiment_completeness_truncate();

COMMENT ON TABLE event_pipeline_watermarks IS
    'Per-project contiguous Redis delivery frontier persisted only after ClickHouse durability and Redis ACK';
COMMENT ON TABLE experiment_analysis_boundaries IS
    'Immutable experiment window plus a deterministic marker in its project event stream';
COMMENT ON TABLE experiment_analysis_snapshots IS
    'Immutable verified experiment decision payload frozen at one covered stream boundary';
