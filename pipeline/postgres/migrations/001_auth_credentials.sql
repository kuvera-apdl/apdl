CREATE TABLE IF NOT EXISTS auth_credentials (
    credential_id TEXT PRIMARY KEY
        CHECK (credential_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    key_hash CHAR(64) NOT NULL UNIQUE
        CHECK (key_hash ~ '^[0-9a-f]{64}$'),
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
    active BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    CHECK ((active AND revoked_at IS NULL) OR NOT active)
);

CREATE INDEX IF NOT EXISTS auth_credentials_project_idx
    ON auth_credentials (project_id)
    WHERE active;
