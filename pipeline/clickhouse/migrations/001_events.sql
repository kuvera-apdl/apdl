-- Migration 001: Events table
CREATE TABLE IF NOT EXISTS events (
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
) ENGINE = MergeTree()
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, event_name, user_id, timestamp)
TTL event_date + INTERVAL 12 MONTH;

-- Materialized columns for common property extractions
ALTER TABLE events ADD COLUMN IF NOT EXISTS page_url String
    MATERIALIZED JSONExtractString(properties, 'page_url');
ALTER TABLE events ADD COLUMN IF NOT EXISTS revenue Float64
    MATERIALIZED JSONExtractFloat(properties, 'revenue');
