-- Migration 015: make server receipt time the retention authority.
--
-- Client event time remains queryable, but it must not choose physical
-- partitions or extend/shorten legal retention. Partition-key expressions
-- cannot be changed in place, so rebuild the two event tables atomically and
-- then restore every projection that consumes the canonical events table.
DROP TABLE IF EXISTS feature_flag_exposures_mv;
DROP TABLE IF EXISTS frontend_health_events_mv;
DROP TABLE IF EXISTS identity_alias_assertions_mv;
DROP TABLE IF EXISTS experiment_event_deliveries_mv;

DROP TABLE IF EXISTS events__apdl_migration_015;
CREATE TABLE events__apdl_migration_015 (
    project_id          String,
    event_id            UUID DEFAULT generateUUIDv4(),
    message_id          String,
    event_type          LowCardinality(String),
    event_name          LowCardinality(String),
    user_id             String,
    anonymous_id        String,
    group_id            String,
    session_id          String,
    timestamp           DateTime64(3),
    received_at         DateTime64(3),
    properties          String,
    traits              String,
    context             String,
    ip                  String,
    country             LowCardinality(String) DEFAULT '',
    region              LowCardinality(String) DEFAULT '',
    device_type         LowCardinality(String) DEFAULT '',
    browser             LowCardinality(String) DEFAULT '',
    source_stream       String DEFAULT '',
    source_stream_id    String DEFAULT '',
    source_stream_id_ms UInt64 DEFAULT 0,
    source_stream_id_seq UInt64 DEFAULT 0,
    event_date          Date DEFAULT toDate(received_at),
    page_url            String MATERIALIZED JSONExtractString(properties, 'page_url'),
    revenue             Float64 MATERIALIZED JSONExtractFloat(properties, 'revenue')
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY project_id
ORDER BY (project_id, message_id)
TTL event_date + INTERVAL 12 MONTH;

INSERT INTO events__apdl_migration_015 (
    project_id,
    event_id,
    message_id,
    event_type,
    event_name,
    user_id,
    anonymous_id,
    group_id,
    session_id,
    timestamp,
    received_at,
    properties,
    traits,
    context,
    ip,
    country,
    region,
    device_type,
    browser,
    source_stream,
    source_stream_id,
    source_stream_id_ms,
    source_stream_id_seq,
    event_date
)
SELECT
    project_id,
    event_id,
    message_id,
    event_type,
    event_name,
    user_id,
    anonymous_id,
    group_id,
    session_id,
    timestamp,
    received_at,
    properties,
    traits,
    context,
    ip,
    country,
    region,
    device_type,
    browser,
    source_stream,
    source_stream_id,
    source_stream_id_ms,
    source_stream_id_seq,
    toDate(received_at)
FROM events;

EXCHANGE TABLES events AND events__apdl_migration_015;
DROP TABLE events__apdl_migration_015;

DROP TABLE IF EXISTS experiment_event_deliveries__apdl_migration_015;
CREATE TABLE experiment_event_deliveries__apdl_migration_015 (
    project_id           String,
    message_id           String,
    event_type           LowCardinality(String),
    event_name           LowCardinality(String),
    user_id              String,
    anonymous_id         String,
    session_id           String,
    timestamp            DateTime64(3),
    received_at          DateTime64(3),
    properties           String,
    source_stream        String,
    source_stream_id     String,
    source_stream_id_ms  UInt64,
    source_stream_id_seq UInt64,
    event_date           Date DEFAULT toDate(received_at)
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY project_id
ORDER BY (
    project_id,
    source_stream,
    source_stream_id
)
TTL event_date + INTERVAL 12 MONTH;

INSERT INTO experiment_event_deliveries__apdl_migration_015 (
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
    event_date
)
SELECT
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
    toDate(received_at)
FROM experiment_event_deliveries;

EXCHANGE TABLES experiment_event_deliveries
    AND experiment_event_deliveries__apdl_migration_015;
DROP TABLE experiment_event_deliveries__apdl_migration_015;

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

CREATE MATERIALIZED VIEW frontend_health_events_mv
TO frontend_health_events
AS SELECT
    project_id,
    message_id,
    event_name,
    user_id,
    anonymous_id,
    session_id,
    timestamp,
    JSONExtractString(properties, 'page') AS page,
    JSONExtractString(properties, 'error_type') AS error_type,
    JSONExtractString(properties, 'component') AS component,
    JSONExtractString(properties, 'slot_id') AS slot_id,
    JSONExtractString(properties, 'source') AS source,
    JSONExtractString(properties, 'message') AS message,
    JSONExtractString(properties, 'metric') AS metric,
    JSONExtract(properties, 'value', 'Nullable(Float64)') AS metric_value,
    JSONExtract(properties, 'delta', 'Nullable(Float64)') AS metric_delta,
    JSONExtractString(properties, 'rating') AS rating,
    JSONExtractString(properties, 'navigation_type') AS navigation_type,
    JSONExtractRaw(properties, 'active_flags') AS active_flags,
    JSONExtractRaw(properties, 'active_flag_versions') AS active_flag_versions,
    toDate(timestamp) AS event_date
FROM events
WHERE event_name IN ('$frontend_error', '$web_vital');

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
    toDate(received_at) AS event_date
FROM events
WHERE source_stream_id != '';
