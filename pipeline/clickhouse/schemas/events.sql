-- Reference schema for the events table
-- Applied via migrations, this file is for documentation

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
    page_url       String MATERIALIZED JSONExtractString(properties, 'page_url'),
    revenue        Float64 MATERIALIZED JSONExtractFloat(properties, 'revenue'),
    event_date     Date DEFAULT toDate(timestamp)
) ENGINE = MergeTree()
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, event_name, user_id, timestamp)
TTL event_date + INTERVAL 12 MONTH;
