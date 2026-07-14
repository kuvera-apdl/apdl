CREATE TABLE IF NOT EXISTS admin_projects (
    project_id TEXT PRIMARY KEY
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    created_by UUID REFERENCES admin_users(user_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO admin_projects (project_id)
SELECT project_id FROM auth_credentials
UNION
SELECT project_id FROM admin_user_projects
ON CONFLICT (project_id) DO NOTHING;

CREATE OR REPLACE FUNCTION ensure_admin_project_exists()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO admin_projects (project_id)
    VALUES (NEW.project_id)
    ON CONFLICT (project_id) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS auth_credentials_ensure_admin_project
    ON auth_credentials;
CREATE TRIGGER auth_credentials_ensure_admin_project
BEFORE INSERT OR UPDATE OF project_id ON auth_credentials
FOR EACH ROW EXECUTE FUNCTION ensure_admin_project_exists();

DROP TRIGGER IF EXISTS admin_user_projects_ensure_admin_project
    ON admin_user_projects;
CREATE TRIGGER admin_user_projects_ensure_admin_project
BEFORE INSERT OR UPDATE OF project_id ON admin_user_projects
FOR EACH ROW EXECUTE FUNCTION ensure_admin_project_exists();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'auth_credentials_admin_project_fk'
          AND conrelid = 'auth_credentials'::regclass
    ) THEN
        ALTER TABLE auth_credentials
            ADD CONSTRAINT auth_credentials_admin_project_fk
            FOREIGN KEY (project_id) REFERENCES admin_projects(project_id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'admin_user_projects_admin_project_fk'
          AND conrelid = 'admin_user_projects'::regclass
    ) THEN
        ALTER TABLE admin_user_projects
            ADD CONSTRAINT admin_user_projects_admin_project_fk
            FOREIGN KEY (project_id) REFERENCES admin_projects(project_id);
    END IF;
END;
$$;
