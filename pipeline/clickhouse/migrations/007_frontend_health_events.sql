-- Migration 007: Frontend health event projection
CREATE TABLE IF NOT EXISTS frontend_health_events (
    project_id             String,
    event_name             LowCardinality(String),
    user_id                String,
    anonymous_id           String,
    session_id             String,
    timestamp              DateTime64(3),
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
    event_date             Date DEFAULT toDate(timestamp)
) ENGINE = MergeTree()
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, event_name, page, timestamp, session_id);

CREATE MATERIALIZED VIEW IF NOT EXISTS frontend_health_events_mv
TO frontend_health_events
AS SELECT
    project_id,
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
