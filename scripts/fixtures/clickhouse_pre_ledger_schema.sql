-- Representative APDL schema immediately before delivery identity and the
-- checksummed ClickHouse migration ledger were introduced.
CREATE TABLE events (
    project_id     String,
    event_id       UUID DEFAULT generateUUIDv4(),
    event_name     LowCardinality(String),
    user_id        String,
    anonymous_id   String,
    session_id     String,
    timestamp      DateTime64(3),
    properties     String,
    country        LowCardinality(String) DEFAULT '',
    region         LowCardinality(String) DEFAULT '',
    device_type    LowCardinality(String) DEFAULT '',
    browser        LowCardinality(String) DEFAULT '',
    event_date     Date DEFAULT toDate(timestamp)
) ENGINE = MergeTree
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, event_name, user_id, timestamp)
TTL event_date + INTERVAL 12 MONTH;

ALTER TABLE events ADD COLUMN page_url String
    MATERIALIZED JSONExtractString(properties, 'page_url');
ALTER TABLE events ADD COLUMN revenue Float64
    MATERIALIZED JSONExtractFloat(properties, 'revenue');

CREATE TABLE sessions (
    project_id     UInt32,
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
    session_date   Date DEFAULT toDate(start_time)
) ENGINE = MergeTree
PARTITION BY (project_id, toYYYYMM(session_date))
ORDER BY (project_id, user_id, start_time);

INSERT INTO sessions (
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
    device_type
)
SELECT
    toUInt32(42),
    'legacy-session',
    'user-1',
    'anonymous-1',
    toDateTime64('2026-07-01 12:00:00', 3),
    toDateTime64('2026-07-01 12:05:00', 3),
    toUInt64(300000),
    toUInt32(2),
    toUInt32(1),
    '/',
    '/',
    '',
    'desktop';

CREATE TABLE feature_flag_exposures (
    project_id           String,
    flag_key             String,
    user_id              String,
    anonymous_id         String,
    session_id           String,
    variant              LowCardinality(String),
    reason               LowCardinality(String),
    rule_id              String,
    rollout_bucket       Nullable(Float64),
    variant_bucket       Nullable(Float64),
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
    variant,
    page
);

CREATE MATERIALIZED VIEW feature_flag_exposures_mv
TO feature_flag_exposures
AS SELECT
    project_id,
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
    timestamp AS first_exposure,
    toDate(timestamp) AS event_date
FROM events
WHERE event_name = '$feature_flag_exposure';

CREATE TABLE frontend_health_events (
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
) ENGINE = MergeTree
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, event_name, page, timestamp, session_id);

CREATE MATERIALIZED VIEW frontend_health_events_mv
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

-- Disconnected prototype objects were once shipped as migrations despite
-- having no writer or reader. They are seeded here to verify upgrade cleanup.
CREATE TABLE events_v2 (
    project_id String,
    idempotency_key String
) ENGINE = MergeTree
ORDER BY (project_id, idempotency_key);

CREATE TABLE events_dlq_v2 (
    project_id String,
    raw_payload String
) ENGINE = MergeTree
ORDER BY project_id;

CREATE TABLE decisions_v2 (
    project_id String,
    schema_name LowCardinality(String),
    idempotency_key String
) ENGINE = MergeTree
ORDER BY (project_id, idempotency_key);

CREATE VIEW flag_evaluations_v AS
SELECT * FROM decisions_v2 WHERE schema_name = 'flag_eval@1';
CREATE VIEW experiment_exposures_v AS
SELECT * FROM decisions_v2 WHERE schema_name = 'exposure@1';
CREATE VIEW agent_actions_v AS
SELECT * FROM decisions_v2 WHERE schema_name = 'agent_action@1';
CREATE VIEW personalizations_v AS
SELECT * FROM decisions_v2 WHERE schema_name = 'personalization@1';

CREATE TABLE feeds_v2 (
    project_id String,
    idempotency_key String
) ENGINE = MergeTree
ORDER BY (project_id, idempotency_key);

INSERT INTO events (
    project_id,
    event_id,
    event_name,
    user_id,
    anonymous_id,
    session_id,
    timestamp,
    properties,
    country,
    region,
    device_type,
    browser
)
SELECT
    'demo',
    toUUID('11111111-1111-1111-1111-111111111111'),
    '$feature_flag_exposure',
    'user-1',
    'anonymous-1',
    'session-1',
    toDateTime64('2026-07-01 12:00:00', 3),
    '{"flag_key":"checkout","variant":"on","reason":"rollout","rule_id":"rule-1","rollout_bucket":1.0,"variant_bucket":2.0,"rollout_percentage":50.0,"bucket_by":"user_id","config_version":3,"source":"sdk","page":"/","component":"hero"}',
    '',
    '',
    'desktop',
    'Chrome';

INSERT INTO events (
    project_id,
    event_id,
    event_name,
    user_id,
    anonymous_id,
    session_id,
    timestamp,
    properties,
    country,
    region,
    device_type,
    browser
)
SELECT
    'demo',
    toUUID('22222222-2222-2222-2222-222222222222'),
    '$frontend_error',
    'user-1',
    'anonymous-1',
    'session-1',
    toDateTime64('2026-07-01 12:01:00', 3),
    '{"page":"/","error_type":"render","component":"hero","slot_id":"main","source":"sdk","message":"boom","metric":"","active_flags":[],"active_flag_versions":{}}',
    '',
    '',
    'desktop',
    'Chrome';
