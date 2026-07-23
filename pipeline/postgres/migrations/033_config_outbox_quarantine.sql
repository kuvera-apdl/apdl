-- Migration 033: terminal Config outbox quarantine and delivery evidence.
--
-- A permanently invalid row or an exhausted retry budget must stop blocking
-- its project lane. The original payload stays in config_outbox alongside a
-- machine-readable failure classification and bounded error evidence.

ALTER TABLE config_outbox
    ADD COLUMN IF NOT EXISTS quarantined_at TIMESTAMPTZ;
ALTER TABLE config_outbox
    ADD COLUMN IF NOT EXISTS failure_class TEXT;
ALTER TABLE config_outbox
    ADD COLUMN IF NOT EXISTS failure_code TEXT;

ALTER TABLE config_outbox
    ADD CONSTRAINT config_outbox_terminal_state_check CHECK (
        processed_at IS NULL OR quarantined_at IS NULL
    );
ALTER TABLE config_outbox
    ADD CONSTRAINT config_outbox_quarantine_evidence_check CHECK (
        (
            quarantined_at IS NULL
            AND failure_class IS NULL
            AND failure_code IS NULL
        )
        OR (
            quarantined_at IS NOT NULL
            AND failure_class IN ('permanent', 'attempts_exhausted')
            AND failure_code IS NOT NULL
            AND btrim(failure_code) <> ''
            AND last_error <> ''
        )
    );

DROP INDEX IF EXISTS idx_config_outbox_pending;
CREATE INDEX idx_config_outbox_pending
    ON config_outbox (available_at, id)
    WHERE processed_at IS NULL AND quarantined_at IS NULL;

CREATE INDEX idx_config_outbox_quarantined
    ON config_outbox (quarantined_at DESC, id DESC)
    WHERE quarantined_at IS NOT NULL;

COMMENT ON COLUMN config_outbox.quarantined_at IS
    'Terminal failure time; quarantined rows retain payload and no longer block a lane';
COMMENT ON COLUMN config_outbox.failure_class IS
    'Canonical terminal class: permanent or attempts_exhausted';
COMMENT ON COLUMN config_outbox.failure_code IS
    'Bounded machine-readable terminal failure code';
