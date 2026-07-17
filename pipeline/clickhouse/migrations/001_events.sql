-- Migration 001: canonical developer-preview events table
CREATE TABLE IF NOT EXISTS events (
    project_id     String,
    event_id       UUID DEFAULT generateUUIDv4(),
    message_id     String,
    event_type     LowCardinality(String),
    event_name     LowCardinality(String),
    user_id        String,
    anonymous_id   String,
    group_id       String,
    session_id     String,
    timestamp      DateTime64(3),
    received_at    DateTime64(3),
    properties     String,
    traits         String,
    context        String,
    ip             String,
    country        LowCardinality(String) DEFAULT '',
    region         LowCardinality(String) DEFAULT '',
    device_type    LowCardinality(String) DEFAULT '',
    browser        LowCardinality(String) DEFAULT '',
    event_date     Date DEFAULT toDate(timestamp)
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, message_id)
TTL event_date + INTERVAL 12 MONTH;

-- Materialized columns for common property extractions
ALTER TABLE events ADD COLUMN IF NOT EXISTS page_url String
    MATERIALIZED JSONExtractString(properties, 'page_url');
ALTER TABLE events ADD COLUMN IF NOT EXISTS revenue Float64
    MATERIALIZED JSONExtractFloat(properties, 'revenue');
