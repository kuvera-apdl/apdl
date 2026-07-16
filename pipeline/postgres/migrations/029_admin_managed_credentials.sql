-- Durable reveal-once credentials created by authenticated human project
-- members. The management role is intentionally valid only on human
-- memberships; service credentials cannot carry it.

ALTER TABLE admin_user_projects
    DROP CONSTRAINT IF EXISTS admin_user_projects_roles_check;
ALTER TABLE admin_user_projects
    ADD CONSTRAINT admin_user_projects_roles_check
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
            'agents:approve',
            'credentials:manage'
        ]::TEXT[]
    );

UPDATE admin_user_projects AS membership
SET roles = membership.roles || ARRAY['credentials:manage']::TEXT[]
FROM admin_projects AS project
WHERE project.project_id = membership.project_id
  AND project.created_by = membership.user_id
  AND NOT ('credentials:manage' = ANY(membership.roles));

ALTER TABLE auth_credentials
    ADD CONSTRAINT auth_credentials_id_project_unique
    UNIQUE (credential_id, project_id);

CREATE TABLE admin_managed_credentials (
    credential_id TEXT PRIMARY KEY
        CHECK (credential_id ~ '^managed-[0-9a-f]{32}$'),
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    created_by_user_id UUID NOT NULL,
    created_by_email TEXT NOT NULL
        CHECK (
            created_by_email = LOWER(created_by_email)
            AND created_by_email
                ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
        ),
    rotated_from_credential_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_managed_credentials_identity_unique
        UNIQUE (credential_id, project_id),
    CONSTRAINT admin_managed_credentials_auth_fk
        FOREIGN KEY (credential_id, project_id)
        REFERENCES auth_credentials(credential_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT admin_managed_credentials_rotated_from_fk
        FOREIGN KEY (rotated_from_credential_id, project_id)
        REFERENCES admin_managed_credentials(credential_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT admin_managed_credentials_rotation_not_self
        CHECK (
            rotated_from_credential_id IS NULL
            OR rotated_from_credential_id <> credential_id
        )
);

CREATE UNIQUE INDEX admin_managed_credentials_one_successor_idx
    ON admin_managed_credentials (rotated_from_credential_id)
    WHERE rotated_from_credential_id IS NOT NULL;

CREATE INDEX admin_managed_credentials_project_created_idx
    ON admin_managed_credentials (project_id, created_at DESC);

CREATE OR REPLACE FUNCTION apdl_validate_managed_credential()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_validate_managed_credential$
DECLARE
    credential auth_credentials%ROWTYPE;
    membership_roles TEXT[];
    canonical_roles TEXT[];
    predecessor auth_credentials%ROWTYPE;
BEGIN
    SELECT stored.*
    INTO credential
    FROM auth_credentials AS stored
    WHERE stored.credential_id = NEW.credential_id
      AND stored.project_id = NEW.project_id
    FOR KEY SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = '23503',
            MESSAGE = 'managed credential requires an existing credential';
    END IF;

    IF credential.expires_at IS NOT NULL
       OR NOT credential.active
       OR credential.revoked_at IS NOT NULL
       OR credential.actor_user_id IS NOT NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'managed SDK credentials must be durable, active, and non-delegated';
    END IF;

    IF credential.credential_kind = 'browser' THEN
        canonical_roles := ARRAY['events:write', 'config:read']::TEXT[];
    ELSE
        canonical_roles := ARRAY(
            SELECT allowed_role
            FROM unnest(
                ARRAY[
                    'events:write',
                    'config:read',
                    'config:evaluate',
                    'query:read'
                ]::TEXT[]
            ) AS allowed_role
            WHERE allowed_role = ANY(credential.roles)
        );
    END IF;

    IF cardinality(canonical_roles) = 0
       OR credential.roles IS DISTINCT FROM canonical_roles THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'managed credential roles are not canonical';
    END IF;

    SELECT membership.roles
    INTO membership_roles
    FROM admin_user_projects AS membership
    JOIN admin_users AS account
      ON account.user_id = membership.user_id
    WHERE membership.user_id = NEW.created_by_user_id
      AND membership.project_id = NEW.project_id
      AND account.active
    FOR KEY SHARE OF membership, account;

    IF NOT FOUND
       OR NOT ('credentials:manage' = ANY(membership_roles))
       OR NOT (credential.roles <@ membership_roles) THEN
        RAISE EXCEPTION USING
            ERRCODE = '42501',
            MESSAGE = 'managed credential exceeds current human membership';
    END IF;

    IF NEW.rotated_from_credential_id IS NOT NULL THEN
        SELECT stored.*
        INTO predecessor
        FROM admin_managed_credentials AS managed
        JOIN auth_credentials AS stored
          ON stored.credential_id = managed.credential_id
         AND stored.project_id = managed.project_id
        WHERE managed.credential_id = NEW.rotated_from_credential_id
          AND managed.project_id = NEW.project_id
        FOR KEY SHARE OF managed, stored;

        IF NOT FOUND
           OR predecessor.credential_kind <> credential.credential_kind
           OR predecessor.roles IS DISTINCT FROM credential.roles THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'credential rotation must preserve kind and roles';
        END IF;
    END IF;

    RETURN NEW;
END
$apdl_validate_managed_credential$;

CREATE TRIGGER admin_managed_credentials_validate
BEFORE INSERT ON admin_managed_credentials
FOR EACH ROW
EXECUTE FUNCTION apdl_validate_managed_credential();

CREATE TABLE admin_credential_audit (
    audit_id UUID PRIMARY KEY,
    project_id TEXT NOT NULL
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    credential_id TEXT NOT NULL,
    action TEXT NOT NULL
        CHECK (action IN ('create', 'rotate', 'revoke')),
    actor_user_id UUID NOT NULL,
    actor_email TEXT NOT NULL
        CHECK (
            actor_email = LOWER(actor_email)
            AND actor_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
        ),
    credential_kind TEXT NOT NULL
        CHECK (credential_kind IN ('confidential', 'browser')),
    roles TEXT[] NOT NULL
        CHECK (
            cardinality(roles) > 0
            AND array_position(roles, NULL) IS NULL
            AND roles <@ ARRAY[
                'events:write',
                'config:read',
                'config:evaluate',
                'query:read'
            ]::TEXT[]
        ),
    successor_credential_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT admin_credential_audit_credential_fk
        FOREIGN KEY (credential_id, project_id)
        REFERENCES admin_managed_credentials(credential_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT admin_credential_audit_successor_fk
        FOREIGN KEY (successor_credential_id, project_id)
        REFERENCES admin_managed_credentials(credential_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT admin_credential_audit_rotation_shape
        CHECK (
            (
                action = 'rotate'
                AND successor_credential_id IS NOT NULL
                AND successor_credential_id <> credential_id
            )
            OR (
                action IN ('create', 'revoke')
                AND successor_credential_id IS NULL
            )
        )
);

CREATE INDEX admin_credential_audit_project_created_idx
    ON admin_credential_audit (project_id, created_at DESC);
CREATE INDEX admin_credential_audit_credential_created_idx
    ON admin_credential_audit (credential_id, created_at DESC);
CREATE INDEX admin_credential_audit_successor_created_idx
    ON admin_credential_audit (successor_credential_id, created_at DESC)
    WHERE successor_credential_id IS NOT NULL;

CREATE OR REPLACE FUNCTION apdl_reject_managed_credential_history_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_managed_credential_history_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = TG_TABLE_NAME || ' history is immutable';
END
$apdl_reject_managed_credential_history_mutation$;

CREATE TRIGGER admin_managed_credentials_no_update_delete
BEFORE UPDATE OR DELETE ON admin_managed_credentials
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_managed_credential_history_mutation();
CREATE TRIGGER admin_managed_credentials_no_truncate
BEFORE TRUNCATE ON admin_managed_credentials
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_managed_credential_history_mutation();

CREATE TRIGGER admin_credential_audit_no_update_delete
BEFORE UPDATE OR DELETE ON admin_credential_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_managed_credential_history_mutation();
CREATE TRIGGER admin_credential_audit_no_truncate
BEFORE TRUNCATE ON admin_credential_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_managed_credential_history_mutation();
