-- Migration 016: one receipt-time retention policy for personal analytics.
--
-- Every persisted table containing user or anonymous identity expires on the
-- calendar date twelve months after the server received the source data.
-- Project-only partitions keep retry identities convergent and prevent event
-- time from choosing physical storage. The redundant irreversible identity
-- aggregate is removed; retained assertions are now the sole resolution source.
DROP TABLE IF EXISTS feature_flag_exposures_mv;
DROP TABLE IF EXISTS frontend_health_events_mv;
DROP TABLE IF EXISTS identity_alias_assertions_mv;
DROP TABLE IF EXISTS experiment_event_deliveries_mv;
DROP VIEW IF EXISTS resolved_identity_aliases;
DROP TABLE IF EXISTS identity_alias_resolution_state_mv;
DROP TABLE IF EXISTS identity_alias_resolution_state;

ALTER TABLE events
    MODIFY TTL toDate(received_at) + INTERVAL 12 MONTH;
ALTER TABLE experiment_event_deliveries
    MODIFY TTL toDate(received_at) + INTERVAL 12 MONTH;

DROP TABLE IF EXISTS sessions__apdl_migration_016;
CREATE TABLE sessions__apdl_migration_016 (
    project_id     String,
    session_id     String,
    user_id        String,
    anonymous_id   String,
    start_time     DateTime64(3),
    end_time       DateTime64(3),
    duration_ms    UInt64,
    event_count    UInt32,
    page_count     UInt32,
    entry_page     String,
    exit_page      String,
    country        LowCardinality(String),
    device_type    LowCardinality(String),
    received_at    DateTime64(3),
    session_date   Date DEFAULT toDate(received_at)
) ENGINE = MergeTree
PARTITION BY project_id
ORDER BY (project_id, user_id, start_time)
TTL toDate(received_at) + INTERVAL 12 MONTH;

INSERT INTO sessions__apdl_migration_016 (
    project_id,
    session_id,
    user_id,
    anonymous_id,
    start_time,
    end_time,
    duration_ms,
    event_count,
    page_count,
    entry_page,
    exit_page,
    country,
    device_type,
    received_at,
    session_date
)
SELECT
    session.project_id,
    session.session_id,
    session.user_id,
    session.anonymous_id,
    session.start_time,
    session.end_time,
    session.duration_ms,
    session.event_count,
    session.page_count,
    session.entry_page,
    session.exit_page,
    session.country,
    session.device_type,
    if(
        receipt.matched_session_id = '',
        now64(3),
        receipt.received_at
    ) AS received_at,
    toDate(
        if(
            receipt.matched_session_id = '',
            now64(3),
            receipt.received_at
        )
    ) AS session_date
FROM sessions AS session
LEFT JOIN (
    SELECT
        project_id,
        session_id AS matched_session_id,
        max(received_at) AS received_at
    FROM events FINAL
    WHERE session_id != ''
    GROUP BY
        project_id,
        session_id
) AS receipt
ON receipt.project_id = session.project_id
AND receipt.matched_session_id = session.session_id;

EXCHANGE TABLES sessions AND sessions__apdl_migration_016;
DROP TABLE sessions__apdl_migration_016;

DROP TABLE IF EXISTS feature_flag_exposures__apdl_migration_016;
CREATE TABLE feature_flag_exposures__apdl_migration_016 (
    project_id          String,
    message_id          String,
    flag_key            String,
    user_id             String,
    anonymous_id        String,
    session_id          String,
    variant             LowCardinality(String),
    reason              LowCardinality(String),
    rule_id             String,
    rollout_bucket      Nullable(Float64),
    variant_bucket      Nullable(Float64),
    rollout_percentage  Nullable(Float64),
    bucket_by           String,
    config_version      UInt32,
    source              LowCardinality(String),
    page                String,
    component           String,
    source_stream       String,
    source_stream_id    String,
    source_stream_id_ms UInt64,
    source_stream_id_seq UInt64,
    first_exposure      DateTime64(3),
    received_at         DateTime64(3),
    event_date          Date DEFAULT toDate(received_at)
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY project_id
ORDER BY (project_id, message_id)
TTL toDate(received_at) + INTERVAL 12 MONTH;

INSERT INTO feature_flag_exposures__apdl_migration_016
SELECT
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
    received_at,
    toDate(received_at) AS event_date
FROM events FINAL
WHERE event_name = '$feature_flag_exposure'
  AND JSONExtractString(properties, 'reason') IN ('rule_match', 'fallthrough');

EXCHANGE TABLES feature_flag_exposures
    AND feature_flag_exposures__apdl_migration_016;
DROP TABLE feature_flag_exposures__apdl_migration_016;

DROP TABLE IF EXISTS frontend_health_events__apdl_migration_016;
CREATE TABLE frontend_health_events__apdl_migration_016 (
    project_id             String,
    message_id             String,
    event_name             LowCardinality(String),
    user_id                String,
    anonymous_id           String,
    session_id             String,
    timestamp              DateTime64(3),
    received_at            DateTime64(3),
    page                   String,
    error_type             LowCardinality(String),
    component              String,
    slot_id                String,
    source                 String,
    message                String,
    metric                 LowCardinality(String),
    metric_value           Nullable(Float64),
    metric_delta           Nullable(Float64),
    rating                 LowCardinality(String),
    navigation_type        LowCardinality(String),
    active_flags           String,
    active_flag_versions   String,
    event_date             Date DEFAULT toDate(received_at)
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY project_id
ORDER BY (project_id, message_id)
TTL toDate(received_at) + INTERVAL 12 MONTH;

INSERT INTO frontend_health_events__apdl_migration_016
SELECT
    project_id,
    message_id,
    event_name,
    user_id,
    anonymous_id,
    session_id,
    timestamp,
    received_at,
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
    toDate(received_at) AS event_date
FROM events FINAL
WHERE event_name IN ('$frontend_error', '$web_vital');

EXCHANGE TABLES frontend_health_events
    AND frontend_health_events__apdl_migration_016;
DROP TABLE frontend_health_events__apdl_migration_016;

DROP TABLE IF EXISTS identity_alias_assertions__apdl_migration_016;
CREATE TABLE identity_alias_assertions__apdl_migration_016 (
    project_id          String,
    message_id          String,
    anonymous_id        String,
    user_id             String,
    identified_at       DateTime64(3),
    received_at         DateTime64(3),
    source_stream       String,
    source_stream_id    String,
    source_stream_id_ms UInt64,
    source_stream_id_seq UInt64,
    event_date          Date DEFAULT toDate(received_at),
    INDEX idx_identity_alias_anonymous_id anonymous_id
        TYPE bloom_filter(0.01) GRANULARITY 4
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY project_id
ORDER BY (project_id, message_id, anonymous_id, user_id)
TTL toDate(received_at) + INTERVAL 12 MONTH;

INSERT INTO identity_alias_assertions__apdl_migration_016
SELECT
    project_id,
    message_id,
    anonymous_id,
    user_id,
    identified_at,
    received_at,
    source_stream,
    source_stream_id,
    source_stream_id_ms,
    source_stream_id_seq,
    toDate(received_at) AS event_date
FROM identity_alias_assertions FINAL;

EXCHANGE TABLES identity_alias_assertions
    AND identity_alias_assertions__apdl_migration_016;
DROP TABLE identity_alias_assertions__apdl_migration_016;

CREATE VIEW resolved_identity_aliases AS
SELECT
    project_id,
    anonymous_id,
    if(min(user_id) = max(user_id), min(user_id), '') AS resolved_user_id,
    min(user_id) != max(user_id) AS has_conflict,
    min(identified_at) AS first_identified_at,
    max(identified_at) AS last_identified_at
FROM identity_alias_assertions FINAL
GROUP BY
    project_id,
    anonymous_id;

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
    received_at,
    toDate(received_at) AS event_date
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
    received_at,
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
    toDate(received_at) AS event_date
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
    source_stream_id_seq,
    toDate(received_at) AS event_date
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
