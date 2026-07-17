-- Migration 005: converge pre-ledger core storage to canonical contracts.
--
-- Older volumes lack delivery identity and use a MergeTree sorting key that
-- cannot be changed to ReplacingMergeTree in place. Add readable defaults for
-- fields the legacy writer never persisted, copy into the canonical engine,
-- then atomically exchange the tables. Derived projections are recreated and
-- backfilled by migrations 006 and 007; identity projection is recreated by
-- migration 011.
DROP TABLE IF EXISTS feature_flag_exposures_mv;
DROP TABLE IF EXISTS frontend_health_events_mv;
DROP TABLE IF EXISTS identity_alias_assertions_mv;

ALTER TABLE events ADD COLUMN IF NOT EXISTS message_id String
    DEFAULT toString(event_id) AFTER event_id;
ALTER TABLE events ADD COLUMN IF NOT EXISTS event_type LowCardinality(String)
    DEFAULT multiIf(
        event_name = 'identify', 'identify',
        event_name = 'group', 'group',
        event_name = 'page', 'page',
        'track'
    ) AFTER message_id;
ALTER TABLE events ADD COLUMN IF NOT EXISTS group_id String
    DEFAULT '' AFTER anonymous_id;
ALTER TABLE events ADD COLUMN IF NOT EXISTS received_at DateTime64(3)
    DEFAULT timestamp AFTER timestamp;
ALTER TABLE events ADD COLUMN IF NOT EXISTS traits String
    DEFAULT '{}' AFTER properties;
ALTER TABLE events ADD COLUMN IF NOT EXISTS context String
    DEFAULT '{}' AFTER traits;
ALTER TABLE events ADD COLUMN IF NOT EXISTS ip String
    DEFAULT '' AFTER context;

DROP TABLE IF EXISTS events__apdl_migration_005;
CREATE TABLE events__apdl_migration_005 (
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
    event_date     Date DEFAULT toDate(timestamp),
    page_url       String MATERIALIZED JSONExtractString(properties, 'page_url'),
    revenue        Float64 MATERIALIZED JSONExtractFloat(properties, 'revenue')
) ENGINE = ReplacingMergeTree(received_at)
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, message_id)
TTL event_date + INTERVAL 12 MONTH;

INSERT INTO events__apdl_migration_005 (
    project_id,
    event_id,
    message_id,
    event_type,
    event_name,
    user_id,
    anonymous_id,
    group_id,
    session_id,
    timestamp,
    received_at,
    properties,
    traits,
    context,
    ip,
    country,
    region,
    device_type,
    browser,
    event_date
)
SELECT
    toString(project_id),
    event_id,
    message_id,
    event_type,
    event_name,
    user_id,
    anonymous_id,
    group_id,
    session_id,
    timestamp,
    received_at,
    properties,
    traits,
    context,
    ip,
    country,
    region,
    device_type,
    browser,
    toDate(timestamp)
FROM events;

EXCHANGE TABLES events AND events__apdl_migration_005;
DROP TABLE events__apdl_migration_005;

-- The earliest sessions table used UInt32 project IDs. A partition-key column
-- cannot be type-modified safely, so use the same atomic rebuild pattern.
DROP TABLE IF EXISTS sessions__apdl_migration_005;
CREATE TABLE sessions__apdl_migration_005 (
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
) ENGINE = MergeTree
PARTITION BY (project_id, toYYYYMM(session_date))
ORDER BY (project_id, user_id, start_time);

INSERT INTO sessions__apdl_migration_005 (
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
    device_type,
    session_date
)
SELECT
    toString(project_id),
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
    device_type,
    toDate(start_time)
FROM sessions;

EXCHANGE TABLES sessions AND sessions__apdl_migration_005;
DROP TABLE sessions__apdl_migration_005;
