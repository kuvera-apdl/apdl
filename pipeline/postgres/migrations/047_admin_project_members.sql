-- Canonical human memberships, reveal-once project invitations, and immutable
-- membership lifecycle audit history.

ALTER TABLE admin_login_rate_buckets
    DROP CONSTRAINT admin_login_rate_buckets_scope_check,
    ADD CONSTRAINT admin_login_rate_buckets_scope_check
        CHECK (
            scope IN (
                'global',
                'network',
                'device',
                'invitation_global',
                'invitation_network',
                'invitation_token'
            )
        ) NOT VALID;
ALTER TABLE admin_login_rate_buckets
    VALIDATE CONSTRAINT admin_login_rate_buckets_scope_check;

CREATE OR REPLACE FUNCTION apdl_canonical_admin_roles(selected_roles TEXT[])
RETURNS TEXT[]
LANGUAGE SQL
IMMUTABLE
STRICT
AS $apdl_canonical_admin_roles$
    SELECT COALESCE(
        ARRAY_AGG(allowed.role ORDER BY allowed.position),
        ARRAY[]::TEXT[]
    )
    FROM UNNEST(
        ARRAY[
            'events:write',
            'config:read',
            'config:write',
            'config:evaluate',
            'query:read',
            'agents:read',
            'agents:run',
            'agents:manage',
            'agents:approve',
            'credentials:manage',
            'members:manage'
        ]::TEXT[]
    ) WITH ORDINALITY AS allowed(role, position)
    WHERE allowed.role = ANY(selected_roles)
$apdl_canonical_admin_roles$;

UPDATE admin_user_projects
SET roles = apdl_canonical_admin_roles(roles);

ALTER TABLE admin_user_projects
    DROP CONSTRAINT admin_user_projects_roles_check;
ALTER TABLE admin_user_projects
    ADD CONSTRAINT admin_user_projects_roles_check
    CHECK (
        cardinality(roles) > 0
        AND roles = apdl_canonical_admin_roles(roles)
    );

CREATE TABLE admin_project_invitations (
    invitation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash CHAR(64) NOT NULL UNIQUE
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    email TEXT NOT NULL
        CHECK (
            email = LOWER(email)
            AND email = BTRIM(email)
            AND LENGTH(email) <= 320
            AND email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'
        ),
    roles TEXT[] NOT NULL
        CHECK (
            cardinality(roles) > 0
            AND roles = apdl_canonical_admin_roles(roles)
        ),
    inviter_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    accepted_by_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at = created_at + INTERVAL '7 days'),
    CHECK (accepted_at IS NULL OR revoked_at IS NULL),
    CHECK ((accepted_at IS NULL) = (accepted_by_user_id IS NULL))
);

CREATE UNIQUE INDEX admin_project_invitations_pending_email_idx
    ON admin_project_invitations (project_id, email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX admin_project_invitations_project_created_idx
    ON admin_project_invitations (project_id, created_at DESC, invitation_id DESC);

CREATE TABLE admin_project_membership_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    action TEXT NOT NULL
        CHECK (
            action IN (
                'invitation_create',
                'invitation_revoke',
                'invitation_accept',
                'roles_replace',
                'member_remove'
            )
        ),
    actor_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    subject_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    subject_email TEXT NOT NULL
        CHECK (
            subject_email = LOWER(subject_email)
            AND subject_email = BTRIM(subject_email)
            AND LENGTH(subject_email) <= 320
        ),
    invitation_id UUID
        REFERENCES admin_project_invitations(invitation_id) ON DELETE RESTRICT,
    previous_roles TEXT[],
    new_roles TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        previous_roles IS NULL
        OR (
            cardinality(previous_roles) > 0
            AND previous_roles = apdl_canonical_admin_roles(previous_roles)
        )
    ),
    CHECK (
        new_roles IS NULL
        OR (
            cardinality(new_roles) > 0
            AND new_roles = apdl_canonical_admin_roles(new_roles)
        )
    ),
    CHECK (
        (
            action = 'invitation_create'
            AND invitation_id IS NOT NULL
            AND subject_user_id IS NULL
            AND previous_roles IS NULL
            AND new_roles IS NOT NULL
        )
        OR (
            action = 'invitation_revoke'
            AND invitation_id IS NOT NULL
            AND subject_user_id IS NULL
            AND previous_roles IS NOT NULL
            AND new_roles IS NULL
        )
        OR (
            action = 'invitation_accept'
            AND invitation_id IS NOT NULL
            AND subject_user_id IS NOT NULL
            AND previous_roles IS NULL
            AND new_roles IS NOT NULL
        )
        OR (
            action = 'roles_replace'
            AND invitation_id IS NULL
            AND subject_user_id IS NOT NULL
            AND previous_roles IS NOT NULL
            AND new_roles IS NOT NULL
            AND previous_roles <> new_roles
        )
        OR (
            action = 'member_remove'
            AND invitation_id IS NULL
            AND subject_user_id IS NOT NULL
            AND previous_roles IS NOT NULL
            AND new_roles IS NULL
        )
    )
);

CREATE INDEX admin_project_membership_audit_project_created_idx
    ON admin_project_membership_audit (project_id, created_at DESC, audit_id DESC);

CREATE OR REPLACE FUNCTION apdl_validate_project_invitation_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_validate_project_invitation_update$
BEGIN
    IF NEW.token_hash <> OLD.token_hash
       OR NEW.project_id <> OLD.project_id
       OR NEW.email <> OLD.email
       OR NEW.roles <> OLD.roles
       OR NEW.inviter_user_id <> OLD.inviter_user_id
       OR NEW.expires_at <> OLD.expires_at
       OR NEW.created_at <> OLD.created_at
       OR OLD.accepted_at IS NOT NULL
       OR OLD.revoked_at IS NOT NULL
       OR (
           (NEW.accepted_at IS NULL AND NEW.revoked_at IS NULL)
           OR (NEW.accepted_at IS NOT NULL AND NEW.revoked_at IS NOT NULL)
       ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'project invitation lifecycle transition is invalid';
    END IF;

    RETURN NEW;
END
$apdl_validate_project_invitation_update$;

CREATE TRIGGER admin_project_invitations_validate_update
BEFORE UPDATE ON admin_project_invitations
FOR EACH ROW
EXECUTE FUNCTION apdl_validate_project_invitation_update();

CREATE OR REPLACE FUNCTION apdl_reject_project_membership_history_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_project_membership_history_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = TG_TABLE_NAME || ' history is immutable';
END
$apdl_reject_project_membership_history_mutation$;

CREATE TRIGGER admin_project_invitations_no_delete
BEFORE DELETE ON admin_project_invitations
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_project_membership_history_mutation();
CREATE TRIGGER admin_project_invitations_no_truncate
BEFORE TRUNCATE ON admin_project_invitations
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_project_membership_history_mutation();

CREATE TRIGGER admin_project_membership_audit_no_update_delete
BEFORE UPDATE OR DELETE ON admin_project_membership_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_project_membership_history_mutation();
CREATE TRIGGER admin_project_membership_audit_no_truncate
BEFORE TRUNCATE ON admin_project_membership_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_project_membership_history_mutation();
