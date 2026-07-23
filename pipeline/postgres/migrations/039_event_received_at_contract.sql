-- Migration 039: converge Config exposure delivery on one receipt-time shape.
--
-- Config writes exposure events directly to the canonical Redis event stream.
-- Pending rows created before server_timestamp became mandatory can use their
-- server-generated event timestamp as the original receipt timestamp.
UPDATE config_outbox
SET payload = jsonb_set(
    payload,
    '{event,server_timestamp}',
    payload #> '{event,timestamp}',
    true
)
WHERE kind = 'exposure'
  AND processed_at IS NULL
  AND quarantined_at IS NULL
  AND jsonb_typeof(payload) = 'object'
  AND jsonb_typeof(payload -> 'event') = 'object'
  AND jsonb_typeof(payload #> '{event,timestamp}') = 'string'
  AND NOT ((payload -> 'event') ? 'server_timestamp');

ALTER TABLE config_exposure_receipts
    ADD CONSTRAINT config_exposure_receipts_server_timestamp_check CHECK (
        NOT ((canonical_payload -> 'event') ? 'server_timestamp')
    );

COMMENT ON COLUMN config_exposure_receipts.canonical_payload IS
    'Exact stream/event exposure payload with generated event times removed';
