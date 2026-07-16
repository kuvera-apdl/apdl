-- One-time recovery of retained alias assertions that predate migration 011.
-- The ClickHouse initializer checks a durable name/checksum ledger before it
-- submits this scan. A crash-safe replay is data-idempotent because assertion
-- retries collapse and the downstream min/max state is a semilattice.
INSERT INTO identity_alias_assertions (
    project_id,
    message_id,
    anonymous_id,
    user_id,
    identified_at,
    received_at
)
SELECT
    project_id,
    message_id,
    anonymous_id,
    user_id,
    timestamp AS identified_at,
    received_at
FROM events FINAL
WHERE event_type = 'identify'
  AND user_id != ''
  AND anonymous_id != '';
