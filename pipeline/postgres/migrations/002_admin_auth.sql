CREATE TABLE IF NOT EXISTS admin_users (
    user_id UUID PRIMARY KEY,
    email TEXT NOT NULL UNIQUE
        CHECK (
            email = LOWER(email)
            AND email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
        ),
    password_hash TEXT NOT NULL
        CHECK (password_hash LIKE '$argon2id$%'),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (failed_login_attempts >= 0),
    locked_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_user_projects (
    user_id UUID NOT NULL REFERENCES admin_users(user_id) ON DELETE CASCADE,
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    roles TEXT[] NOT NULL
        CHECK (
            cardinality(roles) > 0
            AND array_position(roles, NULL) IS NULL
            AND roles <@ ARRAY[
                'events:write',
                'config:read',
                'config:write',
                'config:evaluate',
                'query:read',
                'agents:read',
                'agents:run',
                'agents:manage',
                'agents:approve'
            ]::TEXT[]
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX IF NOT EXISTS admin_user_projects_project_idx
    ON admin_user_projects (project_id);

CREATE TABLE IF NOT EXISTS admin_sessions (
    session_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES admin_users(user_id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    csrf_hash CHAR(64) NOT NULL
        CHECK (csrf_hash ~ '^[0-9a-f]{64}$'),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS admin_sessions_user_idx
    ON admin_sessions (user_id);

CREATE INDEX IF NOT EXISTS admin_sessions_active_idx
    ON admin_sessions (token_hash, expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS admin_proxy_audit (
    audit_id UUID PRIMARY KEY,
    user_id UUID REFERENCES admin_users(user_id) ON DELETE SET NULL,
    actor_email TEXT NOT NULL,
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    required_role TEXT NOT NULL,
    service TEXT NOT NULL
        CHECK (service IN ('ingestion', 'config', 'query', 'agents', 'codegen')),
    method TEXT NOT NULL
        CHECK (method IN ('POST', 'PUT', 'DELETE')),
    path TEXT NOT NULL
        CHECK (path LIKE '/%'),
    status_code INTEGER
        CHECK (status_code BETWEEN 100 AND 599),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS admin_proxy_audit_project_created_idx
    ON admin_proxy_audit (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS admin_proxy_audit_user_created_idx
    ON admin_proxy_audit (user_id, created_at DESC);
