-- Migration 008: feeds_v2 — canonical envelope for external partner feeds.
-- Target: ClickHouse
--
-- This is the landing table for anything ingested from outside APDL: real EDI
-- documents (X12, EDIFACT), partner JSON APIs, CSV drops, webhook payloads.
-- All converted by source-specific adapters into the canonical envelope.
--
-- The raw original document is NOT stored here — it lives in object storage
-- (S3 / MinIO), addressed by content-hash (SHA-256). This table stores the
-- parsed, structured envelope and a pointer back to the raw blob so auditors
-- can always recover the original byte-identical document.

CREATE TABLE IF NOT EXISTS feeds_v2 (
    -- ---------- envelope ----------
    _id                UUID,
    _schema            LowCardinality(String),     -- e.g. 'edi.x12.850@1', 'edi.x12.810@1', 'partner.shipments.csv@1'
    _project_id        UInt32,
    _idempotency_key   String,                     -- e.g. sha256(raw_doc) or vendor-supplied control number
    _correlation_id    UUID,
    _source            LowCardinality(String),     -- e.g. 'edi-adapter@1.0', 'partner-acme@2.1'
    _occurred_at       DateTime64(3),              -- business event time (e.g. PO issue date)
    _received_at       DateTime64(3),              -- when APDL received the feed
    _ingested_at       DateTime64(3) DEFAULT now64(3),

    -- ---------- partner identity ----------
    sender_id          LowCardinality(String) DEFAULT '',   -- X12 ISA06 or partner code
    receiver_id        LowCardinality(String) DEFAULT '',   -- X12 ISA08 or APDL tenant code
    control_number     String DEFAULT '',                   -- ISA13 / GS06 / ST02 if X12

    -- ---------- raw doc pointer ----------
    source_uri         String,                              -- e.g. 's3://apdl-edi-raw/{project}/{yyyy}/{mm}/{dd}/{sha256}.edi'
    source_sha256      FixedString(64),                     -- hex
    source_bytes       UInt64,                              -- size of raw doc

    -- ---------- parsed payload ----------
    payload            String,                              -- parsed envelope payload as JSON
    parse_warnings     Array(String) DEFAULT [],

    -- ---------- derived ----------
    feed_date          Date MATERIALIZED toDate(_occurred_at)
)
ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY (_project_id, toYYYYMM(feed_date))
ORDER BY (_project_id, _idempotency_key)
TTL feed_date + INTERVAL 84 MONTH;        -- 7 years — common regulatory horizon

ALTER TABLE feeds_v2 ADD INDEX IF NOT EXISTS idx_schema _schema
    TYPE set(64) GRANULARITY 4;
ALTER TABLE feeds_v2 ADD INDEX IF NOT EXISTS idx_sender sender_id
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE feeds_v2 ADD INDEX IF NOT EXISTS idx_control control_number
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE feeds_v2 ADD INDEX IF NOT EXISTS idx_sha source_sha256
    TYPE bloom_filter(0.01) GRANULARITY 1;
