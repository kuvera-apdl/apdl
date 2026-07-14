-- Migration 009: Bind Codegen projects to independently verified repositories.
--
-- A project credential proves APDL project authority, not GitHub repository
-- ownership.  The old connection table mixed those two authorities and let a
-- tenant supply a repository slug / installation id directly.  Preserve those
-- rows for audit, but do not grandfather any of them into a write-capable
-- grant.  Every new connection must reference a separately verified grant.

ALTER TABLE codegen_connections
    RENAME TO codegen_connections_legacy_unverified;
ALTER TABLE codegen_connections_legacy_unverified
    ADD COLUMN quarantined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN quarantine_reason TEXT NOT NULL DEFAULT
        'Repository ownership was not independently verified';

CREATE TABLE github_repository_grants (
    grant_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    repository_full_name TEXT NOT NULL,
    status TEXT NOT NULL,
    authorization_source TEXT NOT NULL,
    authorization_subject TEXT NOT NULL,
    verified_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT github_repository_grants_pkey PRIMARY KEY (grant_id),
    CONSTRAINT github_repository_grants_project_grant_key
        UNIQUE (project_id, grant_id),
    CONSTRAINT github_repository_grants_project_fkey
        FOREIGN KEY (project_id) REFERENCES admin_projects (project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT github_repository_grants_grant_id_check CHECK (
        length(grant_id) BETWEEN 5 AND 132
        AND grant_id ~ '^ghg_[A-Za-z0-9_-]+$'
    ),
    CONSTRAINT github_repository_grants_project_id_check CHECK (
        project_id ~ '^[A-Za-z0-9]{1,64}$'
    ),
    CONSTRAINT github_repository_grants_installation_id_check
        CHECK (installation_id > 0),
    CONSTRAINT github_repository_grants_repository_id_check
        CHECK (repository_id > 0),
    CONSTRAINT github_repository_grants_repository_name_check CHECK (
        length(repository_full_name) BETWEEN 3 AND 201
        AND repository_full_name
            ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
    ),
    CONSTRAINT github_repository_grants_status_check CHECK (
        status IN ('pending_reauthorization', 'active', 'revoked')
    ),
    CONSTRAINT github_repository_grants_authorization_source_check CHECK (
        authorization_source IN ('github_oauth', 'operator')
    ),
    CONSTRAINT github_repository_grants_authorization_subject_check CHECK (
        length(authorization_subject) BETWEEN 1 AND 512
        AND btrim(authorization_subject) = authorization_subject
        AND authorization_subject <> ''
        AND position(E'\n' IN authorization_subject) = 0
        AND position(E'\r' IN authorization_subject) = 0
    ),
    CONSTRAINT github_repository_grants_lifecycle_check CHECK (
        (status = 'pending_reauthorization'
            AND verified_at IS NULL AND revoked_at IS NULL)
        OR (status = 'active'
            AND verified_at IS NOT NULL AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

-- One project has one current repository authority.  Revoked grants remain as
-- immutable audit records and continue to identify historical changesets.
CREATE UNIQUE INDEX uq_github_repository_grants_active_project
    ON github_repository_grants (project_id)
    WHERE status = 'active';
CREATE INDEX idx_github_repository_grants_repository
    ON github_repository_grants (repository_id, status);

CREATE FUNCTION enforce_github_repository_grant_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $github_repository_grant_lifecycle$
BEGIN
    IF (OLD.grant_id, OLD.project_id, OLD.installation_id, OLD.repository_id,
        OLD.authorization_source, OLD.authorization_subject, OLD.created_at)
       IS DISTINCT FROM
       (NEW.grant_id, NEW.project_id, NEW.installation_id, NEW.repository_id,
        NEW.authorization_source, NEW.authorization_subject, NEW.created_at)
    THEN
        RAISE EXCEPTION
            'GitHub repository grant identity and evidence are immutable';
    END IF;

    IF OLD.status = 'revoked' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'Revoked GitHub repository grants are immutable';
    END IF;

    IF NOT (
        NEW.status = OLD.status
        OR (OLD.status = 'pending_reauthorization'
            AND NEW.status IN ('active', 'revoked'))
        OR (OLD.status = 'active' AND NEW.status = 'revoked')
    ) THEN
        RAISE EXCEPTION 'Invalid GitHub repository grant transition: % -> %',
            OLD.status, NEW.status;
    END IF;

    IF NEW.status = OLD.status AND (
        NEW.verified_at IS DISTINCT FROM OLD.verified_at
        OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
    ) THEN
        RAISE EXCEPTION
            'GitHub repository grant lifecycle timestamps are immutable';
    END IF;

    -- The slug is display / routing metadata, never repository identity.  It
    -- may follow a GitHub rename while the numeric repository id stays fixed.
    NEW.updated_at := now();
    RETURN NEW;
END
$github_repository_grant_lifecycle$;

CREATE TRIGGER github_repository_grants_enforce_lifecycle
    BEFORE UPDATE ON github_repository_grants
    FOR EACH ROW EXECUTE FUNCTION enforce_github_repository_grant_lifecycle();

CREATE FUNCTION prevent_github_repository_grant_deletion()
RETURNS trigger
LANGUAGE plpgsql
AS $github_repository_grant_deletion$
BEGIN
    RAISE EXCEPTION
        'GitHub repository grants are immutable audit records and cannot be deleted';
END
$github_repository_grant_deletion$;

CREATE TRIGGER github_repository_grants_prevent_delete
    BEFORE DELETE ON github_repository_grants
    FOR EACH ROW EXECUTE FUNCTION prevent_github_repository_grant_deletion();

CREATE TABLE codegen_connections (
    project_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    default_base_branch TEXT NOT NULL DEFAULT 'main',
    tenant_policy JSONB NOT NULL DEFAULT
        '{
          "schema_version":"tenant_codegen_connection_policy@1",
          "test_cmd":null,
          "gates":{
            "max_files":null,
            "max_lines":null,
            "additional_protected_paths":[]
          },
          "runtime_acceptance":{
            "schema_version":"runtime_acceptance_request@1",
            "enabled":false
          }
        }'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT codegen_connections_authorized_pkey PRIMARY KEY (project_id),
    CONSTRAINT codegen_connections_authorized_project_grant_key
        UNIQUE (project_id, grant_id),
    CONSTRAINT codegen_connections_authorized_grant_fkey
        FOREIGN KEY (project_id, grant_id)
        REFERENCES github_repository_grants (project_id, grant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CONSTRAINT codegen_connections_authorized_branch_check CHECK (
        length(default_base_branch) BETWEEN 1 AND 255
        AND btrim(default_base_branch) <> ''
        AND position(E'\n' IN default_base_branch) = 0
        AND position(E'\r' IN default_base_branch) = 0
    ),
    CONSTRAINT codegen_connections_authorized_tenant_policy_check CHECK (
        jsonb_typeof(tenant_policy) = 'object'
        AND tenant_policy->>'schema_version'
            = 'tenant_codegen_connection_policy@1'
    )
);

CREATE FUNCTION require_active_codegen_repository_grant()
RETURNS trigger
LANGUAGE plpgsql
AS $active_codegen_repository_grant$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM github_repository_grants AS grant_record
        WHERE grant_record.project_id = NEW.project_id
          AND grant_record.grant_id = NEW.grant_id
          AND grant_record.status = 'active'
          AND grant_record.verified_at IS NOT NULL
          AND grant_record.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'Codegen connection requires an active same-project repository grant';
    END IF;
    RETURN NEW;
END
$active_codegen_repository_grant$;

CREATE TRIGGER codegen_connections_require_active_grant
    BEFORE INSERT OR UPDATE OF project_id, grant_id ON codegen_connections
    FOR EACH ROW EXECUTE FUNCTION require_active_codegen_repository_grant();

-- Existing changesets have no trustworthy immutable repository identity. Mark
-- them as quarantined in-place so their audit history remains visible, while a
-- defaulted new insert cannot omit its verified repository target.
ALTER TABLE codegen_changesets
    ADD COLUMN repository_grant_id TEXT,
    ADD COLUMN repository_id BIGINT,
    ADD COLUMN repository_installation_id BIGINT,
    ADD COLUMN repository_full_name TEXT,
    ADD COLUMN repository_target_quarantined BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE codegen_changesets
    ALTER COLUMN repository_target_quarantined SET DEFAULT false;

ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_repository_target_shape_check CHECK (
        (repository_target_quarantined
            AND repository_grant_id IS NULL
            AND repository_id IS NULL
            AND repository_installation_id IS NULL
            AND repository_full_name IS NULL)
        OR (NOT repository_target_quarantined
            AND repository_grant_id IS NOT NULL
            AND repository_id IS NOT NULL
            AND repository_id > 0
            AND repository_installation_id IS NOT NULL
            AND repository_installation_id > 0
            AND repository_full_name IS NOT NULL
            AND length(repository_full_name) BETWEEN 3 AND 201
            AND repository_full_name
                ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
    ),
    ADD CONSTRAINT codegen_changesets_repository_grant_fkey
        FOREIGN KEY (project_id, repository_grant_id)
        REFERENCES github_repository_grants (project_id, grant_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT;

CREATE INDEX idx_codegen_changesets_repository_target
    ON codegen_changesets (repository_id, created_at DESC)
    WHERE NOT repository_target_quarantined;

CREATE FUNCTION enforce_codegen_changeset_repository_target()
RETURNS trigger
LANGUAGE plpgsql
AS $codegen_changeset_repository_target$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (OLD.repository_grant_id, OLD.repository_id,
            OLD.repository_installation_id, OLD.repository_full_name,
            OLD.repository_target_quarantined)
           IS DISTINCT FROM
           (NEW.repository_grant_id, NEW.repository_id,
            NEW.repository_installation_id, NEW.repository_full_name,
            NEW.repository_target_quarantined)
        THEN
            RAISE EXCEPTION
                'A changeset repository target is immutable after creation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.repository_target_quarantined THEN
        -- Only rows predating this migration may be quarantined.  The column
        -- default is false after the legacy rows were marked above.
        RAISE EXCEPTION
            'New changesets require a verified repository target';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM github_repository_grants AS grant_record
        WHERE grant_record.project_id = NEW.project_id
          AND grant_record.grant_id = NEW.repository_grant_id
          AND grant_record.installation_id = NEW.repository_installation_id
          AND grant_record.repository_id = NEW.repository_id
          AND grant_record.repository_full_name = NEW.repository_full_name
          AND grant_record.status = 'active'
          AND grant_record.verified_at IS NOT NULL
          AND grant_record.revoked_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'Changeset repository target does not match an active grant';
    END IF;
    RETURN NEW;
END
$codegen_changeset_repository_target$;

CREATE TRIGGER codegen_changesets_enforce_repository_target
    BEFORE INSERT OR UPDATE ON codegen_changesets
    FOR EACH ROW EXECUTE FUNCTION enforce_codegen_changeset_repository_target();
