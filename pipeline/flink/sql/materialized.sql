-- Flink SQL job definitions for real-time materialized views
-- These run as continuous Flink SQL jobs
-- Deploy with: sql-client.sh -f materialized.sql

-- =============================================================================
-- Source: events.enriched Kafka topic
-- =============================================================================
CREATE TABLE events_enriched (
    project_id     STRING,
    event_name     STRING,
    user_id        STRING,
    anonymous_id   STRING,
    session_id     STRING,
    `timestamp`    STRING,
    event_time     AS TO_TIMESTAMP(`timestamp`),
    properties     STRING,
    country        STRING,
    region         STRING,
    context        ROW<device_type STRING, browser STRING>,
    WATERMARK FOR event_time AS event_time - INTERVAL '30' SECOND
) WITH (
    'connector' = 'kafka',
    'topic' = 'events.enriched',
    'properties.bootstrap.servers' = '${KAFKA_BROKERS}',
    'properties.group.id' = 'flink-sql-materialized',
    'scan.startup.mode' = 'latest-offset',
    'format' = 'json',
    'json.fail-on-missing-field' = 'false',
    'json.ignore-parse-errors' = 'true'
);


-- =============================================================================
-- Sink: Real-time event counts (1-minute tumbling window)
-- =============================================================================
CREATE TABLE event_counts_realtime (
    project_id   STRING,
    event_name   STRING,
    window_start TIMESTAMP(3),
    window_end   TIMESTAMP(3),
    event_count  BIGINT,
    unique_users BIGINT,
    PRIMARY KEY (project_id, event_name, window_start) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = '${CLICKHOUSE_JDBC_URL}',
    'table-name' = 'event_counts_realtime',
    'driver' = 'com.clickhouse.jdbc.ClickHouseDriver',
    'sink.buffer-flush.max-rows' = '200',
    'sink.buffer-flush.interval' = '5s',
    'sink.max-retries' = '3'
);

INSERT INTO event_counts_realtime
SELECT
    project_id,
    event_name,
    TUMBLE_START(event_time, INTERVAL '1' MINUTE) AS window_start,
    TUMBLE_END(event_time, INTERVAL '1' MINUTE)   AS window_end,
    COUNT(*)                                       AS event_count,
    COUNT(DISTINCT user_id)                        AS unique_users
FROM events_enriched
GROUP BY
    project_id,
    event_name,
    TUMBLE(event_time, INTERVAL '1' MINUTE);


-- =============================================================================
-- Sink: Hourly event counts
-- =============================================================================
CREATE TABLE event_counts_hourly (
    project_id   STRING,
    event_name   STRING,
    event_hour   TIMESTAMP(3),
    event_count  BIGINT,
    unique_users BIGINT,
    PRIMARY KEY (project_id, event_name, event_hour) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = '${CLICKHOUSE_JDBC_URL}',
    'table-name' = 'event_counts_hourly',
    'driver' = 'com.clickhouse.jdbc.ClickHouseDriver',
    'sink.buffer-flush.max-rows' = '100',
    'sink.buffer-flush.interval' = '10s',
    'sink.max-retries' = '3'
);

INSERT INTO event_counts_hourly
SELECT
    project_id,
    event_name,
    TUMBLE_START(event_time, INTERVAL '1' HOUR) AS event_hour,
    COUNT(*)                                     AS event_count,
    COUNT(DISTINCT user_id)                      AS unique_users
FROM events_enriched
GROUP BY
    project_id,
    event_name,
    TUMBLE(event_time, INTERVAL '1' HOUR);


-- =============================================================================
-- Sink: Daily event counts
-- =============================================================================
CREATE TABLE event_counts_daily (
    project_id   STRING,
    event_name   STRING,
    event_day    DATE,
    event_count  BIGINT,
    unique_users BIGINT,
    PRIMARY KEY (project_id, event_name, event_day) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = '${CLICKHOUSE_JDBC_URL}',
    'table-name' = 'event_counts_daily',
    'driver' = 'com.clickhouse.jdbc.ClickHouseDriver',
    'sink.buffer-flush.max-rows' = '50',
    'sink.buffer-flush.interval' = '30s',
    'sink.max-retries' = '3'
);

INSERT INTO event_counts_daily
SELECT
    project_id,
    event_name,
    CAST(TUMBLE_START(event_time, INTERVAL '1' DAY) AS DATE) AS event_day,
    COUNT(*)                                                   AS event_count,
    COUNT(DISTINCT user_id)                                    AS unique_users
FROM events_enriched
GROUP BY
    project_id,
    event_name,
    TUMBLE(event_time, INTERVAL '1' DAY);


-- =============================================================================
-- Sink: Country-level event breakdown (1-hour window)
-- =============================================================================
CREATE TABLE event_counts_by_country (
    project_id   STRING,
    event_name   STRING,
    country      STRING,
    event_hour   TIMESTAMP(3),
    event_count  BIGINT,
    unique_users BIGINT,
    PRIMARY KEY (project_id, event_name, country, event_hour) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = '${CLICKHOUSE_JDBC_URL}',
    'table-name' = 'event_counts_by_country',
    'driver' = 'com.clickhouse.jdbc.ClickHouseDriver',
    'sink.buffer-flush.max-rows' = '200',
    'sink.buffer-flush.interval' = '10s',
    'sink.max-retries' = '3'
);

INSERT INTO event_counts_by_country
SELECT
    project_id,
    event_name,
    country,
    TUMBLE_START(event_time, INTERVAL '1' HOUR) AS event_hour,
    COUNT(*)                                     AS event_count,
    COUNT(DISTINCT user_id)                      AS unique_users
FROM events_enriched
WHERE country IS NOT NULL AND country <> ''
GROUP BY
    project_id,
    event_name,
    country,
    TUMBLE(event_time, INTERVAL '1' HOUR);


-- =============================================================================
-- Sink: Device-type event breakdown (1-hour window)
-- =============================================================================
CREATE TABLE event_counts_by_device (
    project_id   STRING,
    event_name   STRING,
    device_type  STRING,
    event_hour   TIMESTAMP(3),
    event_count  BIGINT,
    unique_users BIGINT,
    PRIMARY KEY (project_id, event_name, device_type, event_hour) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = '${CLICKHOUSE_JDBC_URL}',
    'table-name' = 'event_counts_by_device',
    'driver' = 'com.clickhouse.jdbc.ClickHouseDriver',
    'sink.buffer-flush.max-rows' = '200',
    'sink.buffer-flush.interval' = '10s',
    'sink.max-retries' = '3'
);

INSERT INTO event_counts_by_device
SELECT
    project_id,
    event_name,
    context.device_type AS device_type,
    TUMBLE_START(event_time, INTERVAL '1' HOUR) AS event_hour,
    COUNT(*)                                     AS event_count,
    COUNT(DISTINCT user_id)                      AS unique_users
FROM events_enriched
WHERE context.device_type IS NOT NULL AND context.device_type <> ''
GROUP BY
    project_id,
    event_name,
    context.device_type,
    TUMBLE(event_time, INTERVAL '1' HOUR);


-- =============================================================================
-- Sink: Revenue metrics (1-hour window)
-- For events that carry a revenue property
-- =============================================================================
CREATE TABLE revenue_metrics_hourly (
    project_id    STRING,
    event_name    STRING,
    event_hour    TIMESTAMP(3),
    event_count   BIGINT,
    unique_users  BIGINT,
    total_revenue DOUBLE,
    PRIMARY KEY (project_id, event_name, event_hour) NOT ENFORCED
) WITH (
    'connector' = 'jdbc',
    'url' = '${CLICKHOUSE_JDBC_URL}',
    'table-name' = 'revenue_metrics_hourly',
    'driver' = 'com.clickhouse.jdbc.ClickHouseDriver',
    'sink.buffer-flush.max-rows' = '100',
    'sink.buffer-flush.interval' = '10s',
    'sink.max-retries' = '3'
);

INSERT INTO revenue_metrics_hourly
SELECT
    project_id,
    event_name,
    TUMBLE_START(event_time, INTERVAL '1' HOUR) AS event_hour,
    COUNT(*)                                     AS event_count,
    COUNT(DISTINCT user_id)                      AS unique_users,
    SUM(CAST(JSON_VALUE(properties, '$.revenue') AS DOUBLE)) AS total_revenue
FROM events_enriched
WHERE JSON_VALUE(properties, '$.revenue') IS NOT NULL
GROUP BY
    project_id,
    event_name,
    TUMBLE(event_time, INTERVAL '1' HOUR);
