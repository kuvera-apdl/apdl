-- Preserve the authoritative Redis delivery identity on analytics rows.
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS source_stream String DEFAULT '' AFTER browser;
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS source_stream_id String DEFAULT '' AFTER source_stream;
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS source_stream_id_ms UInt64 DEFAULT 0 AFTER source_stream_id;
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS source_stream_id_seq UInt64 DEFAULT 0 AFTER source_stream_id_ms;

DROP TABLE IF EXISTS feature_flag_exposures_mv;
DROP TABLE IF EXISTS identity_alias_assertions_mv;

ALTER TABLE feature_flag_exposures
    ADD COLUMN IF NOT EXISTS source_stream String DEFAULT '' AFTER component;
ALTER TABLE feature_flag_exposures
    ADD COLUMN IF NOT EXISTS source_stream_id String DEFAULT '' AFTER source_stream;
ALTER TABLE feature_flag_exposures
    ADD COLUMN IF NOT EXISTS source_stream_id_ms UInt64 DEFAULT 0 AFTER source_stream_id;
ALTER TABLE feature_flag_exposures
    ADD COLUMN IF NOT EXISTS source_stream_id_seq UInt64 DEFAULT 0 AFTER source_stream_id_ms;

CREATE MATERIALIZED VIEW feature_flag_exposures_mv
TO feature_flag_exposures
AS SELECT
    project_id,
    message_id,
    JSONExtractString(properties, 'flag_key') AS flag_key,
    user_id,
    anonymous_id,
    session_id,
    JSONExtractString(properties, 'variant') AS variant,
    JSONExtractString(properties, 'reason') AS reason,
    JSONExtractString(properties, 'rule_id') AS rule_id,
    JSONExtract(properties, 'rollout_bucket', 'Nullable(Float64)') AS rollout_bucket,
    JSONExtract(properties, 'variant_bucket', 'Nullable(Float64)') AS variant_bucket,
    JSONExtract(properties, 'rollout_percentage', 'Nullable(Float64)') AS rollout_percentage,
    JSONExtractString(properties, 'bucket_by') AS bucket_by,
    toUInt32(JSONExtractUInt(properties, 'config_version')) AS config_version,
    JSONExtractString(properties, 'source') AS source,
    JSONExtractString(properties, 'page') AS page,
    JSONExtractString(properties, 'component') AS component,
    source_stream,
    source_stream_id,
    source_stream_id_ms,
    source_stream_id_seq,
    timestamp AS first_exposure,
    toDate(timestamp) AS event_date
FROM events
WHERE event_name = '$feature_flag_exposure'
  AND JSONExtractString(properties, 'reason') IN ('rule_match', 'fallthrough');

ALTER TABLE identity_alias_assertions
    ADD COLUMN IF NOT EXISTS source_stream String DEFAULT '' AFTER received_at;
ALTER TABLE identity_alias_assertions
    ADD COLUMN IF NOT EXISTS source_stream_id String DEFAULT '' AFTER source_stream;
ALTER TABLE identity_alias_assertions
    ADD COLUMN IF NOT EXISTS source_stream_id_ms UInt64 DEFAULT 0 AFTER source_stream_id;
ALTER TABLE identity_alias_assertions
    ADD COLUMN IF NOT EXISTS source_stream_id_seq UInt64 DEFAULT 0 AFTER source_stream_id_ms;

CREATE MATERIALIZED VIEW identity_alias_assertions_mv
TO identity_alias_assertions
AS SELECT
    project_id,
    message_id,
    anonymous_id,
    user_id,
    timestamp AS identified_at,
    received_at,
    source_stream,
    source_stream_id,
    source_stream_id_ms,
    source_stream_id_seq
FROM events
WHERE event_type = 'identify'
  AND user_id != ''
  AND anonymous_id != '';

-- Keep one logical row per Redis delivery. A crash after ClickHouse accepts an
-- INSERT but before Redis ACK is observable will replay the same stream ID.
-- ReplacingMergeTree plus FINAL analysis reads collapse those replays by the
-- writer-authenticated (project, stream, stream-ID) delivery identity. The
-- canonical events table instead replaces by client message ID, so it cannot
-- answer a historical stream-boundary query after a later delivery replaces
-- that client key.
CREATE TABLE experiment_event_deliveries (
    project_id          String,
    message_id          String,
    event_type          LowCardinality(String),
    event_name          LowCardinality(String),
    user_id             String,
    anonymous_id        String,
    session_id          String,
    timestamp           DateTime64(3),
    received_at         DateTime64(3),
    properties          String,
    source_stream       String,
    source_stream_id    String,
    source_stream_id_ms UInt64,
    source_stream_id_seq UInt64,
    event_date          Date DEFAULT toDate(timestamp)
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (
    project_id,
    source_stream,
    source_stream_id
)
TTL event_date + INTERVAL 12 MONTH;

CREATE MATERIALIZED VIEW experiment_event_deliveries_mv
TO experiment_event_deliveries
AS SELECT
    project_id,
    message_id,
    event_type,
    event_name,
    user_id,
    anonymous_id,
    session_id,
    timestamp,
    received_at,
    properties,
    source_stream,
    source_stream_id,
    source_stream_id_ms,
    source_stream_id_seq,
    toDate(timestamp) AS event_date
FROM events
WHERE source_stream_id != '';
