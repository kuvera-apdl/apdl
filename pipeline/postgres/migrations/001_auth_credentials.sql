CREATE TABLE IF NOT EXISTS auth_credentials (
    credential_id TEXT PRIMARY KEY
        CHECK (credential_id ~ '^[A-Za-z0-9_-]{8,64}$'),
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    credential_kind TEXT NOT NULL
        CHECK (credential_kind IN ('confidential', 'browser')),
    key_prefix TEXT NOT NULL
        CHECK (
            (
                credential_kind = 'confidential'
                AND key_prefix = 'proj_' || project_id || '_'
            )
            OR (
                credential_kind = 'browser'
                AND key_prefix = 'client_' || project_id || '_'
            )
        ),
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
            AND cardinality(roles) = (
                ('events:write' = ANY(roles))::INT
                + ('config:read' = ANY(roles))::INT
                + ('config:write' = ANY(roles))::INT
                + ('config:evaluate' = ANY(roles))::INT
                + ('query:read' = ANY(roles))::INT
                + ('agents:read' = ANY(roles))::INT
                + ('agents:run' = ANY(roles))::INT
                + ('agents:manage' = ANY(roles))::INT
                + ('agents:approve' = ANY(roles))::INT
            )
            AND (
                credential_kind = 'confidential'
                OR (
                    credential_kind = 'browser'
                    AND cardinality(roles) = 2
                    AND roles @> ARRAY[
                        'events:write',
                        'config:read'
                    ]::TEXT[]
                    AND roles <@ ARRAY[
                        'events:write',
                        'config:read'
                    ]::TEXT[]
                )
            )
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
