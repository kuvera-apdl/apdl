-- Migration 008: events_v2 — canonical envelope table for behavior events.
-- Target: ClickHouse
-- Apply with: clickhouse-client --multiquery < 008_events_v2.sql
--
-- Notes:
--   * Engine is ReplacingMergeTree keyed on (project_id, idempotency_key) so
--     at-least-once delivery from the Redis Streams consumer cannot create
--     duplicate rows. Queries that must dedup should use FROM events_v2 FINAL
--     or wrap with argMax on _ingested_at.
--   * The envelope columns are prefixed with `_` to mirror the on-the-wire
--     JSON keys (_id, _schema, _idempotency_key, ...). Payload-derived columns
--     have plain names.
--   * The legacy `events` table is left in place; the writer should dual-write
--     until parity is verified, then reads cut over and `events` is dropped.

CREATE TABLE IF NOT EXISTS events_v2 (
    -- ---------- envelope ----------
    _id                UUID,
    _schema            LowCardinality(String),     -- e.g. 'track@1', 'page@1'
    _project_id        UInt32,
    _idempotency_key   String,                     -- = SDK messageId
    _correlation_id    UUID,                       -- causal chain across services
    _source            LowCardinality(String),     -- e.g. 'sdk-js@2.4.1'
    _occurred_at       DateTime64(3),              -- client / business time
    _received_at       DateTime64(3),              -- ingestion server time
    _ingested_at       DateTime64(3) DEFAULT now64(3),
    _ip                IPv6,                       -- IPv4 maps as ::ffff:a.b.c.d

    -- ---------- promoted payload (typed columns) ----------
    event_name         LowCardinality(String),
    user_id            String,
    anonymous_id       String,
    session_id         String,

    -- ---------- flattened context ----------
    country            LowCardinality(String) DEFAULT '',
    region             LowCardinality(String) DEFAULT '',
    device_type        LowCardinality(String) DEFAULT '',
    browser            LowCardinality(String) DEFAULT '',
    os_name            LowCardinality(String) DEFAULT '',
    locale             LowCardinality(String) DEFAULT '',
    page_url           String DEFAULT '',
    referrer           String DEFAULT '',
    sdk_version        LowCardinality(String) DEFAULT '',

    -- ---------- payload tail ----------
    properties         String,                     -- raw JSON, queryable via JSONExtract*
    traits             String DEFAULT '',

    -- ---------- derived ----------
    event_date         Date MATERIALIZED toDate(_occurred_at)
)
ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY (_project_id, toYYYYMM(event_date))
ORDER BY (_project_id, _idempotency_key)
TTL event_date + INTERVAL 12 MONTH;

-- Secondary skip indexes for common analytical filters.
ALTER TABLE events_v2 ADD INDEX IF NOT EXISTS idx_event_name event_name
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE events_v2 ADD INDEX IF NOT EXISTS idx_user_id user_id
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE events_v2 ADD INDEX IF NOT EXISTS idx_correlation _correlation_id
    TYPE bloom_filter(0.01) GRANULARITY 4;

-- Materialized columns: cheap projections of frequently queried payload fields.
-- Add more here as analytics patterns settle (revenue, plan, etc.).
ALTER TABLE events_v2 ADD COLUMN IF NOT EXISTS revenue Float64
    MATERIALIZED JSONExtractFloat(properties, 'revenue');
ALTER TABLE events_v2 ADD COLUMN IF NOT EXISTS plan LowCardinality(String)
    MATERIALIZED JSONExtractString(properties, 'plan');

-- Dead-letter table for envelope-validation failures. Same envelope shape so
-- analysts can investigate bad data without leaving SQL. Short TTL.
CREATE TABLE IF NOT EXISTS events_dlq_v2 (
    _project_id        UInt32,
    _received_at       DateTime64(3) DEFAULT now64(3),
    _source            LowCardinality(String) DEFAULT '',
    error              String,
    raw_payload        String                      -- the rejected JSON, verbatim
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(_received_at)
ORDER BY (_project_id, _received_at)
TTL toDate(_received_at) + INTERVAL 30 DAY;
