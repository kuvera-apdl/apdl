-- Migration 002: Sessions table
CREATE TABLE IF NOT EXISTS sessions (
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
    session_date   Date DEFAULT toDate(start_time)
) ENGINE = MergeTree()
PARTITION BY (project_id, toYYYYMM(session_date))
ORDER BY (project_id, user_id, start_time);
