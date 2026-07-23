SELECT pg_advisory_lock_shared(:maintenance_inhibitor_lock_id);
SELECT pg_advisory_lock_shared(:maintenance_guard_lock_id);

INSERT INTO auth_credentials (
    credential_id, project_id, credential_kind, key_prefix, key_hash, roles
)
VALUES (
    :'credential_id',
    :'project_id',
    :'credential_kind',
    :'key_prefix',
    :'key_hash',
    :'roles'::TEXT[]
)
ON CONFLICT (credential_id) DO UPDATE SET
    project_id = EXCLUDED.project_id,
    credential_kind = EXCLUDED.credential_kind,
    key_prefix = EXCLUDED.key_prefix,
    key_hash = EXCLUDED.key_hash,
    roles = EXCLUDED.roles,
    active = TRUE,
    expires_at = NULL,
    revoked_at = NULL;
