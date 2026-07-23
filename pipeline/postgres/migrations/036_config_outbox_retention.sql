-- Migration 036: bounded Config outbox retention with a durable exposure ledger.
--
-- Exposure message IDs remain conflict-detectable after their delivery intents
-- are pruned.  Receipts retain the canonical payload without the server-
-- generated event timestamp for 400 days: longer than the ClickHouse events
-- TTL of 12 calendar months, with an additional safety margin.

CREATE TABLE config_exposure_receipts (
    project_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    canonical_payload JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, message_id),
    CONSTRAINT config_exposure_receipts_project_check CHECK (
        btrim(project_id) <> ''
    ),
    CONSTRAINT config_exposure_receipts_message_check CHECK (
        btrim(message_id) <> ''
    ),
    CONSTRAINT config_exposure_receipts_payload_check CHECK (
        jsonb_typeof(canonical_payload) = 'object'
        AND jsonb_typeof(canonical_payload -> 'stream_key') = 'string'
        AND btrim(canonical_payload ->> 'stream_key') <> ''
        AND jsonb_typeof(canonical_payload -> 'event') = 'object'
        AND NOT ((canonical_payload -> 'event') ? 'timestamp')
        AND canonical_payload #>> '{event,message_id}' = message_id
    ),
    CONSTRAINT config_exposure_receipts_time_check CHECK (
        last_seen_at >= first_seen_at
    )
);

-- Preserve the existing message-ID conflict authority before any outbox row
-- can become eligible for cleanup.  The outbox unique key guarantees one
-- source row for each project/message pair.
INSERT INTO config_exposure_receipts (
    project_id,
    message_id,
    canonical_payload,
    first_seen_at,
    last_seen_at
)
SELECT
    project_id,
    dedup_key,
    payload #- '{event,timestamp}',
    created_at,
    GREATEST(
        created_at,
        COALESCE(processed_at, created_at),
        COALESCE(quarantined_at, created_at)
    )
FROM config_outbox
WHERE kind = 'exposure';

CREATE INDEX idx_config_exposure_receipts_cleanup
    ON config_exposure_receipts (last_seen_at, project_id, message_id);

CREATE INDEX idx_config_outbox_cleanup_processed
    ON config_outbox (processed_at, id)
    WHERE processed_at IS NOT NULL;

DROP INDEX IF EXISTS idx_config_outbox_quarantined;
CREATE INDEX idx_config_outbox_cleanup_quarantined
    ON config_outbox (quarantined_at, id)
    WHERE quarantined_at IS NOT NULL;

COMMENT ON TABLE config_exposure_receipts IS
    'Exposure idempotency/conflict ledger retained beyond ClickHouse event TTL';
COMMENT ON COLUMN config_exposure_receipts.canonical_payload IS
    'Exact stream/event exposure payload with only event.timestamp removed';
COMMENT ON COLUMN config_exposure_receipts.last_seen_at IS
    'Receipt retention anchor; migration backfill includes terminal delivery time';
