-- Migration 011: durable, tenant-bound anonymous-to-user identity aliases.
--
-- An identify event carrying both canonical IDs is an irreversible alias
-- assertion. Exact delivery retries collapse, while a reused message ID with
-- a different claim remains a separate assertion so conflicts fail closed.
CREATE TABLE IF NOT EXISTS identity_alias_assertions (
    project_id       String,
    message_id       String,
    anonymous_id     String,
    user_id          String,
    identified_at    DateTime64(3),
    received_at      DateTime64(3)
) ENGINE = ReplacingMergeTree(received_at)
ORDER BY (project_id, message_id, anonymous_id, user_id);

ALTER TABLE identity_alias_assertions
    ADD INDEX IF NOT EXISTS idx_identity_alias_anonymous_id anonymous_id
    TYPE bloom_filter(0.01) GRANULARITY 4;

-- Resolution is a monotone min/max semilattice. It stays constant-size per
-- (project, anonymous ID), is insensitive to retry duplication, and records a
-- conflict whenever more than one distinct user has been asserted.
CREATE TABLE IF NOT EXISTS identity_alias_resolution_state (
    project_id          String,
    anonymous_id        String,
    min_user_id         AggregateFunction(min, String),
    max_user_id         AggregateFunction(max, String),
    first_identified_at AggregateFunction(min, DateTime64(3)),
    last_identified_at  AggregateFunction(max, DateTime64(3))
) ENGINE = AggregatingMergeTree
ORDER BY (project_id, anonymous_id);

-- Install the downstream view first so every assertion inserted by the live
-- projection or the one-time backfill contributes to resolution state.
CREATE MATERIALIZED VIEW IF NOT EXISTS identity_alias_resolution_state_mv
TO identity_alias_resolution_state
AS SELECT
    project_id,
    anonymous_id,
    minState(user_id) AS min_user_id,
    maxState(user_id) AS max_user_id,
    minState(identified_at) AS first_identified_at,
    maxState(identified_at) AS last_identified_at
FROM identity_alias_assertions
GROUP BY
    project_id,
    anonymous_id;

CREATE MATERIALIZED VIEW IF NOT EXISTS identity_alias_assertions_mv
TO identity_alias_assertions
AS SELECT
    project_id,
    message_id,
    anonymous_id,
    user_id,
    timestamp AS identified_at,
    received_at
FROM events
WHERE event_type = 'identify'
  AND user_id != ''
  AND anonymous_id != '';

CREATE VIEW IF NOT EXISTS resolved_identity_aliases AS
SELECT
    project_id,
    anonymous_id,
    if(
        minMerge(min_user_id) = maxMerge(max_user_id),
        minMerge(min_user_id),
        ''
    ) AS resolved_user_id,
    minMerge(min_user_id) != maxMerge(max_user_id) AS has_conflict,
    minMerge(first_identified_at) AS first_identified_at,
    maxMerge(last_identified_at) AS last_identified_at
FROM identity_alias_resolution_state
GROUP BY
    project_id,
    anonymous_id;
