-- Migration 007: decisions_v2 — unified analytical store for all "decisions".
-- Target: ClickHouse
--
-- A "decision" is anything produced by APDL itself rather than the user:
--   * flag_eval@1     — Config service evaluated a flag for a user
--   * exposure@1      — user was assigned a variant in a running experiment
--   * agent_action@1  — Agents service proposed/executed an action
--   * personalization@1 — runtime selection of UI/content variant
--
-- All four share the same envelope so a single query can stitch a user's
-- causal chain together (flag eval -> exposure -> agent action -> downstream
-- event). The schema discriminator + a payload JSON column keeps the surface
-- area small while still allowing per-schema materialized columns below.

CREATE TABLE IF NOT EXISTS decisions_v2 (
    -- ---------- envelope ----------
    _id                UUID,
    _schema            LowCardinality(String),     -- flag_eval@1 | exposure@1 | agent_action@1 | personalization@1
    _project_id        UInt32,
    _idempotency_key   String,
    _correlation_id    UUID,
    _source            LowCardinality(String),
    _occurred_at       DateTime64(3),
    _received_at       DateTime64(3),
    _ingested_at       DateTime64(3) DEFAULT now64(3),

    -- ---------- common identity ----------
    user_id            String,
    anonymous_id       String,
    session_id         String DEFAULT '',

    -- ---------- promoted payload fields (sparse — populated per schema) ----------
    flag_key           LowCardinality(String) DEFAULT '',  -- flag_eval@1, exposure@1
    experiment_key     LowCardinality(String) DEFAULT '',  -- exposure@1, agent_action@1
    variant            LowCardinality(String) DEFAULT '',  -- flag_eval@1, exposure@1, personalization@1
    reason             LowCardinality(String) DEFAULT '',  -- flag_eval@1: rule/default/rollout
    rule_id            String DEFAULT '',
    rollout_bucket     UInt16 DEFAULT 0,                   -- MurmurHash3 bucket 0..9999
    action_type        LowCardinality(String) DEFAULT '',  -- agent_action@1: propose_experiment | personalize | ...
    approval_status    LowCardinality(String) DEFAULT '',  -- agent_action@1: auto | approved | rejected
    run_id             UUID DEFAULT toUUID('00000000-0000-0000-0000-000000000000'),

    -- ---------- payload tail ----------
    payload            String,                     -- full JSON of the decision payload
    safety_result      String DEFAULT '',          -- agent_action@1: validator output JSON

    -- ---------- derived ----------
    decision_date      Date MATERIALIZED toDate(_occurred_at)
)
ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY (_project_id, toYYYYMM(decision_date))
ORDER BY (_project_id, _idempotency_key)
TTL decision_date + INTERVAL 24 MONTH;   -- keep longer than events; smaller volume, audit value

ALTER TABLE decisions_v2 ADD INDEX IF NOT EXISTS idx_schema _schema
    TYPE set(16) GRANULARITY 4;
ALTER TABLE decisions_v2 ADD INDEX IF NOT EXISTS idx_user user_id
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE decisions_v2 ADD INDEX IF NOT EXISTS idx_corr _correlation_id
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE decisions_v2 ADD INDEX IF NOT EXISTS idx_flag flag_key
    TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE decisions_v2 ADD INDEX IF NOT EXISTS idx_experiment experiment_key
    TYPE bloom_filter(0.01) GRANULARITY 4;

-- Per-schema views: give analysts/agents a clean per-type surface without
-- forcing them to remember the discriminator filter.
CREATE VIEW IF NOT EXISTS flag_evaluations_v AS
SELECT * FROM decisions_v2 FINAL WHERE _schema = 'flag_eval@1';

CREATE VIEW IF NOT EXISTS experiment_exposures_v AS
SELECT * FROM decisions_v2 FINAL WHERE _schema = 'exposure@1';

CREATE VIEW IF NOT EXISTS agent_actions_v AS
SELECT * FROM decisions_v2 FINAL WHERE _schema = 'agent_action@1';

CREATE VIEW IF NOT EXISTS personalizations_v AS
SELECT * FROM decisions_v2 FINAL WHERE _schema = 'personalization@1';
