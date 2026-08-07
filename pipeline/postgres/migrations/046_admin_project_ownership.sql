-- Human project ownership is separate from immutable creator provenance and
-- from operator-controlled execution authorization.

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
            'credentials:manage',
            'members:manage'
        ]::TEXT[]
    );

UPDATE admin_user_projects AS membership
SET roles = membership.roles || ARRAY['members:manage']::TEXT[]
FROM admin_projects AS project
WHERE project.project_id = membership.project_id
  AND project.created_by = membership.user_id
  AND NOT ('members:manage' = ANY(membership.roles));

ALTER TABLE admin_projects
    ADD COLUMN owner_user_id UUID;
ALTER TABLE admin_projects
    ADD CONSTRAINT admin_projects_owner_user_fk
    FOREIGN KEY (owner_user_id)
    REFERENCES admin_users(user_id)
    ON DELETE RESTRICT;

UPDATE admin_projects
SET owner_user_id = created_by
WHERE created_by IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM admin_projects AS project
        LEFT JOIN admin_users AS account
          ON account.user_id = project.owner_user_id
        LEFT JOIN admin_user_projects AS membership
          ON membership.project_id = project.project_id
         AND membership.user_id = project.owner_user_id
        WHERE project.created_by IS NOT NULL
          AND (
              project.owner_user_id IS NULL
              OR account.user_id IS NULL
              OR NOT account.active
              OR membership.user_id IS NULL
              OR NOT ('members:manage' = ANY(membership.roles))
          )
    ) THEN
        RAISE EXCEPTION
            'self-created projects require an active owner with members:manage';
    END IF;
END;
$$;

CREATE TABLE admin_project_ownership_audit (
    audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    previous_owner_user_id UUID
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    new_owner_user_id UUID NOT NULL
        REFERENCES admin_users(user_id) ON DELETE RESTRICT,
    actor TEXT NOT NULL
        CHECK (
            actor = BTRIM(actor)
            AND LENGTH(actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN actor) = 0
            AND POSITION(CHR(13) IN actor) = 0
        ),
    reason TEXT NOT NULL
        CHECK (
            reason = BTRIM(reason)
            AND LENGTH(reason) BETWEEN 1 AND 2000
            AND POSITION(CHR(10) IN reason) = 0
            AND POSITION(CHR(13) IN reason) = 0
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        previous_owner_user_id IS NULL
        OR previous_owner_user_id <> new_owner_user_id
    )
);

CREATE INDEX admin_project_ownership_audit_project_created_idx
    ON admin_project_ownership_audit (project_id, created_at DESC, audit_id DESC);

CREATE OR REPLACE FUNCTION apdl_validate_project_owner_assignment()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_validate_project_owner_assignment$
DECLARE
    owner_roles TEXT[];
BEGIN
    IF NEW.owner_user_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT membership.roles
    INTO owner_roles
    FROM admin_users AS account
    JOIN admin_user_projects AS membership
      ON membership.user_id = account.user_id
     AND membership.project_id = NEW.project_id
    WHERE account.user_id = NEW.owner_user_id
      AND account.active
    FOR SHARE OF account, membership;

    IF NOT FOUND OR NOT ('members:manage' = ANY(owner_roles)) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'project owner must be an active project member with members:manage';
    END IF;

    RETURN NEW;
END
$apdl_validate_project_owner_assignment$;

CREATE TRIGGER admin_projects_validate_owner
BEFORE INSERT OR UPDATE OF owner_user_id, project_id ON admin_projects
FOR EACH ROW
EXECUTE FUNCTION apdl_validate_project_owner_assignment();

CREATE OR REPLACE FUNCTION apdl_require_self_created_project_owner()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_require_self_created_project_owner$
DECLARE
    current_created_by UUID;
    current_owner_user_id UUID;
BEGIN
    SELECT created_by, owner_user_id
    INTO current_created_by, current_owner_user_id
    FROM admin_projects
    WHERE project_id = NEW.project_id;

    IF current_created_by IS NOT NULL AND current_owner_user_id IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'self-created projects require a human owner';
    END IF;

    RETURN NULL;
END
$apdl_require_self_created_project_owner$;

CREATE CONSTRAINT TRIGGER admin_projects_require_human_owner
AFTER INSERT OR UPDATE OF created_by, owner_user_id ON admin_projects
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION apdl_require_self_created_project_owner();

CREATE OR REPLACE FUNCTION apdl_protect_project_owner_membership()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_protect_project_owner_membership$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
            FROM admin_projects
            WHERE project_id = OLD.project_id
              AND owner_user_id = OLD.user_id
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'project owner membership and members:manage role are required';
        END IF;
        RETURN OLD;
    END IF;

    IF (
        NEW.project_id <> OLD.project_id
        OR NEW.user_id <> OLD.user_id
        OR NOT ('members:manage' = ANY(NEW.roles))
    ) AND EXISTS (
        SELECT 1
        FROM admin_projects
        WHERE project_id = OLD.project_id
          AND owner_user_id = OLD.user_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'project owner membership and members:manage role are required';
    END IF;

    RETURN NEW;
END
$apdl_protect_project_owner_membership$;

CREATE TRIGGER admin_user_projects_protect_owner
BEFORE UPDATE OR DELETE ON admin_user_projects
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_project_owner_membership();

CREATE OR REPLACE FUNCTION apdl_protect_active_project_owner()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_protect_active_project_owner$
BEGIN
    IF OLD.active AND NOT NEW.active AND EXISTS (
        SELECT 1
        FROM admin_projects
        WHERE owner_user_id = OLD.user_id
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'project owner account must remain active';
    END IF;

    RETURN NEW;
END
$apdl_protect_active_project_owner$;

CREATE TRIGGER admin_users_protect_active_owner
BEFORE UPDATE OF active ON admin_users
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_active_project_owner();

CREATE OR REPLACE FUNCTION apdl_reject_project_ownership_audit_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_reject_project_ownership_audit_mutation$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'project ownership audit is immutable';
END
$apdl_reject_project_ownership_audit_mutation$;

CREATE TRIGGER admin_project_ownership_audit_no_update_delete
BEFORE UPDATE OR DELETE ON admin_project_ownership_audit
FOR EACH ROW
EXECUTE FUNCTION apdl_reject_project_ownership_audit_mutation();

CREATE TRIGGER admin_project_ownership_audit_no_truncate
BEFORE TRUNCATE ON admin_project_ownership_audit
FOR EACH STATEMENT
EXECUTE FUNCTION apdl_reject_project_ownership_audit_mutation();
