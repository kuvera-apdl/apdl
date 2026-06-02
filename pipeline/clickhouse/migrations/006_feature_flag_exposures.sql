-- Migration 006: Feature flag exposure projection
CREATE TABLE IF NOT EXISTS feature_flag_exposures (
    project_id           String,
    flag_key             String,
    user_id              String,
    anonymous_id         String,
    session_id           String,
    value                Bool,
    reason               LowCardinality(String),
    rule_id              String,
    bucket               Nullable(Float64),
    rollout_percentage   Nullable(Float64),
    bucket_by            String,
    config_version       UInt32,
    source               LowCardinality(String),
    page                 String,
    component            String,
    first_exposure       DateTime64(3),
    event_date           Date DEFAULT toDate(first_exposure)
) ENGINE = ReplacingMergeTree(first_exposure)
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (
    project_id,
    flag_key,
    user_id,
    anonymous_id,
    session_id,
    config_version,
    value,
    page
);

CREATE MATERIALIZED VIEW IF NOT EXISTS feature_flag_exposures_mv
TO feature_flag_exposures
AS SELECT
    project_id,
    JSONExtractString(properties, 'flag_key') AS flag_key,
    user_id,
    anonymous_id,
    session_id,
    toBool(JSONExtractBool(properties, 'value')) AS value,
    JSONExtractString(properties, 'reason') AS reason,
    JSONExtractString(properties, 'rule_id') AS rule_id,
    JSONExtract(properties, 'bucket', 'Nullable(Float64)') AS bucket,
    JSONExtract(properties, 'rollout_percentage', 'Nullable(Float64)') AS rollout_percentage,
    JSONExtractString(properties, 'bucket_by') AS bucket_by,
    toUInt32(JSONExtractUInt(properties, 'config_version')) AS config_version,
    JSONExtractString(properties, 'source') AS source,
    JSONExtractString(properties, 'page') AS page,
    JSONExtractString(properties, 'component') AS component,
    timestamp AS first_exposure,
    toDate(timestamp) AS event_date
FROM events
WHERE event_name = '$feature_flag_exposure';
