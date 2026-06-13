-- Migration 004: Materialized views for real-time aggregations

-- Hourly event counts
CREATE MATERIALIZED VIEW IF NOT EXISTS event_counts_hourly_mv
ENGINE = SummingMergeTree()
ORDER BY (project_id, event_name, event_hour)
AS SELECT
    project_id,
    event_name,
    toStartOfHour(timestamp) AS event_hour,
    count() AS event_count,
    uniq(user_id) AS unique_users
FROM events
GROUP BY project_id, event_name, event_hour;

-- Daily event counts
CREATE MATERIALIZED VIEW IF NOT EXISTS event_counts_daily_mv
ENGINE = SummingMergeTree()
ORDER BY (project_id, event_name, event_day)
AS SELECT
    project_id,
    event_name,
    toDate(timestamp) AS event_day,
    count() AS event_count,
    uniq(user_id) AS unique_users
FROM events
GROUP BY project_id, event_name, event_day;
