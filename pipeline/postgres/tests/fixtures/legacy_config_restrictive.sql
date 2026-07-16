-- Config schema emitted by an older startup-DDL build. Its lifecycle checks
-- deliberately reject the canonical values introduced by migration 006.
CREATE TABLE flags (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft',
    enabled BOOLEAN NOT NULL DEFAULT false,
    description TEXT NOT NULL DEFAULT '',
    disabled_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT flags_state_check CHECK (state IN ('draft', 'active')),
    CONSTRAINT flags_state_enabled_check CHECK ((state = 'active') = enabled),
    PRIMARY KEY (project_id, key)
);
INSERT INTO flags (
    key, project_id, state, enabled, description, disabled_reason
) VALUES (
    'legacy-disabled', 'apdl', 'draft', false, 'legacy', 'guardrail_failed'
);

CREATE TABLE experiments (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    variants_json TEXT NOT NULL DEFAULT '[]',
    targeting_rules_json TEXT NOT NULL DEFAULT '[]',
    traffic_percentage DOUBLE PRECISION NOT NULL DEFAULT 100.0,
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT experiments_status_check
        CHECK (status IN ('draft', 'active', 'completed', 'stopped')),
    PRIMARY KEY (project_id, key)
);
INSERT INTO experiments (key, project_id, status)
VALUES ('legacy-active', 'apdl', 'active');

CREATE TABLE feature_flags (
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    default_value BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (project_id, key)
);
INSERT INTO feature_flags (project_id, key)
VALUES ('apdl', 'preserve-me');
