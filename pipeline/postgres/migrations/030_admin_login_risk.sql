ALTER TABLE admin_users
    DROP COLUMN IF EXISTS failed_login_attempts,
    DROP COLUMN IF EXISTS locked_until;

CREATE TABLE admin_login_rate_buckets (
    scope TEXT NOT NULL
        CHECK (scope IN ('global', 'network', 'device')),
    key_hash CHAR(64) NOT NULL
        CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    window_started_at TIMESTAMPTZ NOT NULL,
    attempt_count INTEGER NOT NULL
        CHECK (attempt_count > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope, key_hash)
);

CREATE INDEX admin_login_rate_buckets_updated_idx
    ON admin_login_rate_buckets (updated_at);

CREATE TABLE admin_login_source_risk (
    scope TEXT NOT NULL
        CHECK (scope IN ('network', 'device')),
    source_hash CHAR(64) NOT NULL
        CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    email_hash CHAR(64) NOT NULL
        CHECK (email_hash ~ '^[0-9a-f]{64}$'),
    failure_count INTEGER NOT NULL
        CHECK (failure_count > 0),
    next_allowed_at TIMESTAMPTZ NOT NULL,
    last_failed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope, source_hash, email_hash)
);

CREATE INDEX admin_login_source_risk_updated_idx
    ON admin_login_source_risk (updated_at);

CREATE TABLE admin_login_account_risk (
    user_id UUID PRIMARY KEY
        REFERENCES admin_users(user_id) ON DELETE CASCADE,
    email_hash CHAR(64) NOT NULL
        CHECK (email_hash ~ '^[0-9a-f]{64}$'),
    window_started_at TIMESTAMPTZ NOT NULL,
    failure_count INTEGER NOT NULL
        CHECK (failure_count > 0),
    last_failed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX admin_login_account_risk_email_idx
    ON admin_login_account_risk (email_hash);

CREATE TABLE admin_security_notifications (
    notification_id UUID PRIMARY KEY,
    user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE CASCADE,
    kind TEXT NOT NULL
        CHECK (kind = 'suspicious_login_activity'),
    status TEXT NOT NULL DEFAULT 'unread'
        CHECK (status IN ('unread', 'acknowledged')),
    observed_failures INTEGER NOT NULL
        CHECK (observed_failures > 0),
    window_started_at TIMESTAMPTZ NOT NULL,
    last_detected_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    CHECK (
        (status = 'unread' AND acknowledged_at IS NULL)
        OR (status = 'acknowledged' AND acknowledged_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX admin_security_notifications_unread_login_idx
    ON admin_security_notifications (user_id, kind)
    WHERE status = 'unread';

CREATE INDEX admin_security_notifications_user_created_idx
    ON admin_security_notifications (user_id, created_at DESC);
