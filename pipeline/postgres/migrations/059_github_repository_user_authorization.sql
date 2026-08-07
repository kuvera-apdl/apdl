-- Migration 059: project-scoped GitHub App user authorization for Codegen
-- repositories.
--
-- The browser never supplies GitHub installation coordinates as authority.
-- A short-lived authorization flow stores only a hash of the OAuth state and
-- server-derived repository candidates.  Completing a flow records the APDL
-- and GitHub users that proved access to the exact numeric repository id.

ALTER TABLE public.github_repository_grants
    ADD COLUMN authorized_by_user_id UUID,
    ADD COLUMN github_user_id BIGINT;

-- Migration 009 admitted `github_oauth` as a source label but recorded neither
-- the APDL actor nor immutable GitHub user id.  Those rows cannot honestly be
-- upgraded into the strict proof introduced here.  Relabel and terminally
-- revoke them before installing the new evidence constraints.  Existing
-- connections/changesets fail closed because every execution lookup requires
-- an active grant; the project owner must complete the new user flow again.
ALTER TABLE public.github_repository_grants
    DROP CONSTRAINT github_repository_grants_authorization_source_check,
    ADD CONSTRAINT github_repository_grants_authorization_source_check CHECK (
        authorization_source IN (
            'github_oauth',
            'operator',
            'legacy_unverified'
        )
    );

ALTER TABLE public.github_repository_grants
    DISABLE TRIGGER github_repository_grants_enforce_lifecycle;

UPDATE public.github_repository_grants
SET authorization_source = 'legacy_unverified',
    status = 'revoked',
    revoked_at = COALESCE(revoked_at, now()),
    updated_at = now()
WHERE authorization_source = 'github_oauth';

ALTER TABLE public.github_repository_grants
    ENABLE TRIGGER github_repository_grants_enforce_lifecycle;

ALTER TABLE public.github_repository_grants
    ADD CONSTRAINT github_repository_grants_user_evidence_check CHECK (
        (
            authorization_source = 'operator'
            AND authorized_by_user_id IS NULL
            AND github_user_id IS NULL
        ) OR (
            authorization_source = 'legacy_unverified'
            AND authorized_by_user_id IS NULL
            AND github_user_id IS NULL
        ) OR (
            authorization_source = 'github_oauth'
            AND authorized_by_user_id IS NOT NULL
            AND github_user_id IS NOT NULL
            AND github_user_id > 0
        )
    ),
    ADD CONSTRAINT github_repository_grants_legacy_quarantine_check CHECK (
        authorization_source <> 'legacy_unverified'
        OR (
            status = 'revoked'
            AND revoked_at IS NOT NULL
            AND authorized_by_user_id IS NULL
            AND github_user_id IS NULL
        )
    ),
    ADD CONSTRAINT github_repository_grants_oauth_subject_check CHECK (
        authorization_source <> 'github_oauth'
        OR (
            github_user_id IS NOT NULL
            AND authorization_subject = 'github_user:' || github_user_id::TEXT
        )
    ),
    ADD CONSTRAINT github_repository_grants_authorized_user_fkey
        FOREIGN KEY (authorized_by_user_id)
        REFERENCES public.admin_users(user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT;

CREATE INDEX idx_github_repository_grants_authorized_user
    ON public.github_repository_grants (authorized_by_user_id, project_id)
    WHERE authorized_by_user_id IS NOT NULL;

CREATE TABLE public.github_repository_authorization_flows (
    authorization_id UUID NOT NULL,
    project_id TEXT NOT NULL,
    actor_user_id UUID NOT NULL,
    state_hash CHAR(64) NOT NULL,
    status TEXT NOT NULL,
    github_user_id BIGINT,
    github_login TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT github_repository_authorization_flows_pkey
        PRIMARY KEY (authorization_id),
    CONSTRAINT github_repository_authorization_flows_state_key
        UNIQUE (state_hash),
    CONSTRAINT github_repository_authorization_flows_project_id_check
        CHECK (project_id ~ '^[A-Za-z0-9]{1,64}$'),
    CONSTRAINT github_repository_authorization_flows_state_hash_check
        CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT github_repository_authorization_flows_status_check
        CHECK (status IN (
            'awaiting_installation',
            'awaiting_oauth',
            'awaiting_selection',
            'completed'
        )),
    CONSTRAINT github_repository_authorization_flows_github_user_id_check
        CHECK (github_user_id IS NULL OR github_user_id > 0),
    CONSTRAINT github_repository_authorization_flows_github_login_check
        CHECK (
            github_login IS NULL OR (
                length(github_login) BETWEEN 1 AND 39
                AND github_login ~ '^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$'
            )
        ),
    CONSTRAINT github_repository_authorization_flows_expiry_check
        CHECK (expires_at > created_at),
    CONSTRAINT github_repository_authorization_flows_lifecycle_check CHECK (
        (
            status = 'awaiting_installation'
            AND github_user_id IS NULL
            AND github_login IS NULL
            AND completed_at IS NULL
        ) OR (
            status = 'awaiting_oauth'
            AND github_user_id IS NULL
            AND github_login IS NULL
            AND completed_at IS NULL
        ) OR (
            status = 'awaiting_selection'
            AND github_user_id IS NOT NULL
            AND github_login IS NOT NULL
            AND completed_at IS NULL
        ) OR (
            status = 'completed'
            AND github_user_id IS NOT NULL
            AND github_login IS NOT NULL
            AND completed_at IS NOT NULL
        )
    ),
    CONSTRAINT github_repository_authorization_flows_project_fkey
        FOREIGN KEY (project_id)
        REFERENCES public.admin_projects(project_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT github_repository_authorization_flows_actor_fkey
        FOREIGN KEY (actor_user_id)
        REFERENCES public.admin_users(user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

CREATE INDEX idx_github_repository_authorization_flows_actor
    ON public.github_repository_authorization_flows
        (project_id, actor_user_id, created_at DESC);

CREATE INDEX idx_github_repository_authorization_flows_expiry
    ON public.github_repository_authorization_flows (expires_at);

CREATE TABLE public.github_repository_authorization_candidates (
    candidate_id UUID NOT NULL,
    authorization_id UUID NOT NULL,
    installation_id BIGINT NOT NULL,
    repository_id BIGINT NOT NULL,
    repository_full_name TEXT NOT NULL,
    default_base_branch TEXT NOT NULL,
    private BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT github_repository_authorization_candidates_pkey
        PRIMARY KEY (candidate_id),
    CONSTRAINT github_repository_authorization_candidates_flow_repository_key
        UNIQUE (authorization_id, repository_id),
    CONSTRAINT github_repo_auth_candidates_installation_id_check
        CHECK (installation_id > 0),
    CONSTRAINT github_repository_authorization_candidates_repository_id_check
        CHECK (repository_id > 0),
    CONSTRAINT github_repo_auth_candidates_repository_name_check
        CHECK (
            length(repository_full_name) BETWEEN 3 AND 201
            AND repository_full_name ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'
        ),
    CONSTRAINT github_repository_authorization_candidates_branch_check
        CHECK (
            length(default_base_branch) BETWEEN 1 AND 255
            AND btrim(default_base_branch) <> ''
            AND position(chr(10) IN default_base_branch) = 0
            AND position(chr(13) IN default_base_branch) = 0
        ),
    CONSTRAINT github_repository_authorization_candidates_flow_fkey
        FOREIGN KEY (authorization_id)
        REFERENCES public.github_repository_authorization_flows(authorization_id)
        ON UPDATE RESTRICT
        ON DELETE CASCADE
);

CREATE INDEX idx_github_repository_authorization_candidates_flow
    ON public.github_repository_authorization_candidates
        (authorization_id, lower(repository_full_name));

CREATE FUNCTION public.prevent_github_repository_authorization_candidate_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'GitHub repository authorization candidates are immutable';
END
$$;

CREATE TRIGGER github_repository_authorization_candidates_immutable
BEFORE UPDATE ON public.github_repository_authorization_candidates
FOR EACH ROW
EXECUTE FUNCTION public.prevent_github_repository_authorization_candidate_update();

CREATE OR REPLACE FUNCTION public.enforce_github_repository_grant_lifecycle()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF (OLD.grant_id, OLD.project_id, OLD.installation_id, OLD.repository_id,
        OLD.authorization_source, OLD.authorization_subject,
        OLD.authorized_by_user_id, OLD.github_user_id, OLD.created_at)
       IS DISTINCT FROM
       (NEW.grant_id, NEW.project_id, NEW.installation_id, NEW.repository_id,
        NEW.authorization_source, NEW.authorization_subject,
        NEW.authorized_by_user_id, NEW.github_user_id, NEW.created_at)
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

    -- The slug is display / routing metadata, never repository identity. It
    -- may follow a GitHub rename while the numeric repository id stays fixed.
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.github_repository_authorization_flows
TO apdl_runtime;

GRANT SELECT, INSERT, DELETE ON
    public.github_repository_authorization_candidates
TO apdl_runtime;

-- Migration 044's ALTER DEFAULT PRIVILEGES grants UPDATE on new tables. Remove
-- that unused authority explicitly; the trigger protects against other roles.
REVOKE UPDATE ON
    public.github_repository_authorization_candidates
FROM apdl_runtime;

GRANT SELECT (
    authorized_by_user_id,
    github_user_id
) ON public.github_repository_grants TO apdl_runtime;

GRANT INSERT (
    authorized_by_user_id,
    github_user_id
) ON public.github_repository_grants TO apdl_runtime;
