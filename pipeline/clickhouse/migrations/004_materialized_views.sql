-- Migration 004: Retire duplicate-amplifying aggregate materialized views.
--
-- ReplacingMergeTree resolves retry duplicates only when the source is read with
-- FINAL. Materialized views process each inserted block before replacement, so
-- SummingMergeTree would permanently add retried events a second time. Supported
-- analytics aggregate directly from `events FINAL` instead.
DROP TABLE IF EXISTS event_counts_hourly_mv;
DROP TABLE IF EXISTS event_counts_daily_mv;
