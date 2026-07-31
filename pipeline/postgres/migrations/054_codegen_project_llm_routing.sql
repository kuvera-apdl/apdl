-- Bind Codegen changesets and every provider egress to immutable project LLM
-- assignments and exact credential authority.

CREATE TABLE codegen_project_model_assignments (
    project_id TEXT NOT NULL
        REFERENCES admin_projects(project_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('editor', 'helper')),
    provider TEXT NOT NULL
        CHECK (provider IN ('anthropic', 'openai', 'google', 'xai')),
    model_id TEXT NOT NULL,
    assignment_version BIGINT NOT NULL CHECK (assignment_version > 0),
    connection_version BIGINT NOT NULL CHECK (connection_version > 0),
    inventory_version BIGINT NOT NULL CHECK (inventory_version > 0),
    catalog_version TEXT NOT NULL
        CHECK (catalog_version ~ '^codegen-provider-catalog@[1-9][0-9]*$'),
    assigned_by_actor TEXT NOT NULL
        CHECK (
            assigned_by_actor = BTRIM(assigned_by_actor)
            AND LENGTH(assigned_by_actor) BETWEEN 1 AND 512
            AND POSITION(CHR(10) IN assigned_by_actor) = 0
            AND POSITION(CHR(13) IN assigned_by_actor) = 0
        ),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT codegen_project_model_assignments_pkey
        PRIMARY KEY (project_id, role),
    CONSTRAINT codegen_project_model_assignments_model_fk
        FOREIGN KEY (
            project_id, provider, model_id, connection_version,
            inventory_version, catalog_version
        )
        REFERENCES codegen_project_provider_models (
            project_id, provider, model_id, connection_version,
            inventory_version, catalog_version
        ) ON DELETE NO ACTION
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX codegen_project_model_assignments_provider_idx
    ON codegen_project_model_assignments (project_id, provider, model_id);

CREATE OR REPLACE FUNCTION apdl_validate_codegen_model_assignment()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM codegen_project_provider_connections AS connection
        JOIN codegen_project_provider_models AS model
          ON model.project_id = connection.project_id
         AND model.provider = connection.provider
         AND model.connection_version = connection.version
         AND model.inventory_version = connection.inventory_version
         AND model.catalog_version = connection.catalog_version
        WHERE connection.project_id = NEW.project_id
          AND connection.provider = NEW.provider
          AND connection.state = 'active'
          AND model.model_id = NEW.model_id
          AND NEW.role = ANY(model.supported_roles)
          AND NEW.connection_version = connection.version
          AND NEW.inventory_version = connection.inventory_version
          AND NEW.catalog_version = connection.catalog_version
          AND NEW.catalog_version = model.catalog_version
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Codegen model assignment requires an eligible current inventory model';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER codegen_project_model_assignments_validate
BEFORE INSERT OR UPDATE ON codegen_project_model_assignments
FOR EACH ROW
EXECUTE FUNCTION apdl_validate_codegen_model_assignment();

CREATE OR REPLACE FUNCTION apdl_assert_codegen_model_assignments_current(
    expected_project_id TEXT,
    expected_provider TEXT
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM codegen_project_provider_connections AS connection
        JOIN codegen_project_provider_models AS model
          ON model.project_id = connection.project_id
         AND model.provider = connection.provider
         AND model.connection_version = connection.version
         AND model.inventory_version = connection.inventory_version
         AND model.catalog_version = connection.catalog_version
        WHERE connection.project_id = expected_project_id
          AND connection.provider = expected_provider
          AND connection.state = 'revoked'
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Revoked Codegen provider connection must not retain model inventory';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM codegen_project_model_assignments AS assignment
        WHERE assignment.project_id = expected_project_id
          AND assignment.provider = expected_provider
          AND NOT EXISTS (
              SELECT 1
              FROM codegen_project_provider_connections AS connection
              JOIN codegen_project_provider_models AS model
                ON model.project_id = connection.project_id
               AND model.provider = connection.provider
               AND model.connection_version = connection.version
               AND model.inventory_version = connection.inventory_version
               AND model.catalog_version = connection.catalog_version
              WHERE connection.project_id = assignment.project_id
                AND connection.provider = assignment.provider
                AND connection.state = 'active'
                AND model.model_id = assignment.model_id
                AND assignment.role = ANY(model.supported_roles)
                AND assignment.connection_version = connection.version
                AND assignment.inventory_version = connection.inventory_version
                AND assignment.catalog_version = connection.catalog_version
                AND assignment.catalog_version = model.catalog_version
          )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Codegen provider mutation would leave a stale model assignment';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION apdl_revalidate_codegen_model_assignments()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        PERFORM apdl_assert_codegen_model_assignments_current(
            NEW.project_id,
            NEW.provider
        );
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM apdl_assert_codegen_model_assignments_current(
            OLD.project_id,
            OLD.provider
        );
    ELSE
        PERFORM apdl_assert_codegen_model_assignments_current(
            OLD.project_id,
            OLD.provider
        );
        IF (NEW.project_id, NEW.provider)
           IS DISTINCT FROM (OLD.project_id, OLD.provider) THEN
            PERFORM apdl_assert_codegen_model_assignments_current(
                NEW.project_id,
                NEW.provider
            );
        END IF;
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER codegen_connections_revalidate_model_assignments
AFTER INSERT OR UPDATE OR DELETE
ON codegen_project_provider_connections
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION apdl_revalidate_codegen_model_assignments();

CREATE CONSTRAINT TRIGGER codegen_models_revalidate_model_assignments
AFTER INSERT OR UPDATE OR DELETE
ON codegen_project_provider_models
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION apdl_revalidate_codegen_model_assignments();

ALTER TABLE codegen_changesets
    ADD COLUMN llm_execution_snapshot JSONB;

CREATE OR REPLACE FUNCTION apdl_is_codegen_llm_assignment_snapshot(
    assignment JSONB,
    expected_role TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(assignment) = 'object'
        AND assignment ?& ARRAY[
            'schema_version', 'role', 'provider', 'model_id',
            'assignment_version', 'connection_version', 'inventory_version',
            'catalog_version', 'context_window_tokens',
            'supports_tool_calling', 'supports_structured_output',
            'input_cost_per_million_tokens_usd_micros',
            'output_cost_per_million_tokens_usd_micros'
        ]
        AND assignment - ARRAY[
            'schema_version', 'role', 'provider', 'model_id',
            'assignment_version', 'connection_version', 'inventory_version',
            'catalog_version', 'context_window_tokens',
            'supports_tool_calling', 'supports_structured_output',
            'input_cost_per_million_tokens_usd_micros',
            'output_cost_per_million_tokens_usd_micros'
        ] = '{}'::JSONB
        AND jsonb_typeof(assignment->'schema_version') = 'string'
        AND assignment->>'schema_version'
            = 'codegen_llm_assignment_snapshot@1'
        AND jsonb_typeof(assignment->'role') = 'string'
        AND assignment->>'role' = expected_role
        AND jsonb_typeof(assignment->'provider') = 'string'
        AND assignment->>'provider'
            IN ('anthropic', 'openai', 'google', 'xai')
        AND jsonb_typeof(assignment->'model_id') = 'string'
        AND assignment->>'model_id'
            ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'
        AND jsonb_typeof(assignment->'assignment_version') = 'number'
        AND assignment->>'assignment_version' ~ '^[1-9][0-9]*$'
        AND (assignment->>'assignment_version')::BIGINT > 0
        AND jsonb_typeof(assignment->'connection_version') = 'number'
        AND assignment->>'connection_version' ~ '^[1-9][0-9]*$'
        AND (assignment->>'connection_version')::BIGINT > 0
        AND jsonb_typeof(assignment->'inventory_version') = 'number'
        AND assignment->>'inventory_version' ~ '^[1-9][0-9]*$'
        AND (assignment->>'inventory_version')::BIGINT > 0
        AND jsonb_typeof(assignment->'catalog_version') = 'string'
        AND assignment->>'catalog_version'
            ~ '^codegen-provider-catalog@[1-9][0-9]*$'
        AND jsonb_typeof(assignment->'context_window_tokens') = 'number'
        AND assignment->>'context_window_tokens' ~ '^[1-9][0-9]*$'
        AND (assignment->>'context_window_tokens')::BIGINT >= 16000
        AND jsonb_typeof(assignment->'supports_tool_calling') = 'boolean'
        AND jsonb_typeof(assignment->'supports_structured_output') = 'boolean'
        AND jsonb_typeof(
            assignment->'input_cost_per_million_tokens_usd_micros'
        ) = 'number'
        AND (
            assignment->>'input_cost_per_million_tokens_usd_micros'
        ) ~ '^(0|[1-9][0-9]*)$'
        AND (
            assignment->>'input_cost_per_million_tokens_usd_micros'
        )::BIGINT >= 0
        AND jsonb_typeof(
            assignment->'output_cost_per_million_tokens_usd_micros'
        ) = 'number'
        AND (
            assignment->>'output_cost_per_million_tokens_usd_micros'
        ) ~ '^(0|[1-9][0-9]*)$'
        AND (
            assignment->>'output_cost_per_million_tokens_usd_micros'
        )::BIGINT >= 0
    ) IS TRUE
$$;

CREATE OR REPLACE FUNCTION apdl_is_codegen_llm_execution_snapshot(
    snapshot JSONB,
    expected_project_id TEXT,
    expected_grant_id TEXT,
    expected_repository_id BIGINT,
    expected_installation_id BIGINT,
    expected_repository_full_name TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL
IMMUTABLE
STRICT
AS $$
    SELECT (
        jsonb_typeof(snapshot) = 'object'
        AND snapshot ?& ARRAY[
            'schema_version', 'project_id', 'repository_grant_id',
            'repository_id', 'repository_installation_id',
            'repository_full_name', 'codegen_revision',
            'behavior_configuration_sha256', 'rollout_stage', 'assignments'
        ]
        AND snapshot - ARRAY[
            'schema_version', 'project_id', 'repository_grant_id',
            'repository_id', 'repository_installation_id',
            'repository_full_name', 'codegen_revision',
            'behavior_configuration_sha256', 'rollout_stage', 'assignments'
        ] = '{}'::JSONB
        AND jsonb_typeof(snapshot->'schema_version') = 'string'
        AND snapshot->>'schema_version'
            = 'codegen_llm_execution_snapshot@1'
        AND jsonb_typeof(snapshot->'project_id') = 'string'
        AND snapshot->>'project_id' = expected_project_id
        AND jsonb_typeof(snapshot->'repository_grant_id') = 'string'
        AND snapshot->>'repository_grant_id' = expected_grant_id
        AND jsonb_typeof(snapshot->'repository_id') = 'number'
        AND snapshot->>'repository_id' ~ '^[1-9][0-9]*$'
        AND (snapshot->>'repository_id')::BIGINT = expected_repository_id
        AND jsonb_typeof(snapshot->'repository_installation_id') = 'number'
        AND snapshot->>'repository_installation_id' ~ '^[1-9][0-9]*$'
        AND (snapshot->>'repository_installation_id')::BIGINT
            = expected_installation_id
        AND jsonb_typeof(snapshot->'repository_full_name') = 'string'
        AND snapshot->>'repository_full_name'
            = expected_repository_full_name
        AND jsonb_typeof(snapshot->'codegen_revision') = 'string'
        AND LENGTH(snapshot->>'codegen_revision') BETWEEN 1 AND 200
        AND jsonb_typeof(
            snapshot->'behavior_configuration_sha256'
        ) = 'string'
        AND snapshot->>'behavior_configuration_sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(snapshot->'rollout_stage') = 'string'
        AND snapshot->>'rollout_stage' IN (
            'offline', 'shadow', 'development_pr', 'reviewed_pr',
            'low_risk_canary'
        )
        AND jsonb_typeof(snapshot->'assignments') = 'array'
        AND jsonb_array_length(snapshot->'assignments') = 2
        AND apdl_is_codegen_llm_assignment_snapshot(
            snapshot->'assignments'->0, 'editor'
        )
        AND apdl_is_codegen_llm_assignment_snapshot(
            snapshot->'assignments'->1, 'helper'
        )
    ) IS TRUE
$$;

ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_llm_execution_snapshot_check CHECK (
        llm_execution_snapshot IS NULL
        OR apdl_is_codegen_llm_execution_snapshot(
            llm_execution_snapshot,
            project_id,
            repository_grant_id,
            repository_id,
            repository_installation_id,
            repository_full_name
        )
    );

CREATE OR REPLACE FUNCTION apdl_protect_codegen_llm_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.llm_execution_snapshot IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'New Codegen changesets require an LLM execution snapshot';
    END IF;
    IF TG_OP = 'UPDATE'
       AND NEW.llm_execution_snapshot IS DISTINCT FROM OLD.llm_execution_snapshot THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Codegen LLM execution snapshot is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER codegen_changesets_protect_llm_snapshot
BEFORE INSERT OR UPDATE OF llm_execution_snapshot ON codegen_changesets
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_codegen_llm_snapshot();

ALTER TABLE codegen_changesets
    ADD CONSTRAINT codegen_changesets_attempt_identity_key
    UNIQUE (changeset_id, project_id);

CREATE TABLE codegen_llm_attempts (
    attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL,
    changeset_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('brief', 'edit', 'review', 'repair')),
    role TEXT NOT NULL CHECK (role IN ('editor', 'helper')),
    attempt_sequence INTEGER NOT NULL CHECK (attempt_sequence > 0),
    provider TEXT NOT NULL
        CHECK (provider IN ('anthropic', 'openai', 'google', 'xai')),
    model_id TEXT NOT NULL,
    assignment_version BIGINT NOT NULL CHECK (assignment_version > 0),
    credential_id UUID,
    credential_version BIGINT,
    status TEXT NOT NULL CHECK (
        status IN (
            'prepared', 'in_flight', 'succeeded', 'failed', 'blocked',
            'cancelled'
        )
    ),
    egress_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    latency_ms BIGINT CHECK (latency_ms IS NULL OR latency_ms >= 0),
    input_tokens BIGINT CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens IS NULL OR output_tokens >= 0),
    cost_usd_micros BIGINT CHECK (
        cost_usd_micros IS NULL OR cost_usd_micros >= 0
    ),
    error_classification TEXT CHECK (
        error_classification IS NULL OR error_classification IN (
            'changeset_unavailable', 'execution_authority_unavailable',
            'repository_authority_unavailable',
            'rollout_authority_unavailable',
            'credential_unavailable', 'credential_replaced',
            'credential_revoked', 'credential_authentication',
            'connection_unavailable', 'model_unavailable',
            'provider_authentication', 'provider_permission',
            'provider_rate_limited', 'provider_timeout',
            'provider_unavailable', 'provider_invalid_request',
            'provider_safety_block', 'cancelled', 'unknown'
        )
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT codegen_llm_attempts_changeset_fk
        FOREIGN KEY (changeset_id, project_id)
        REFERENCES codegen_changesets (changeset_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT codegen_llm_attempts_credential_fk
        FOREIGN KEY (
            credential_id, project_id, provider, credential_version
        )
        REFERENCES codegen_project_provider_credentials (
            credential_id, project_id, provider, credential_version
        ) ON DELETE RESTRICT,
    CONSTRAINT codegen_llm_attempts_sequence_key
        UNIQUE (changeset_id, phase, attempt_sequence),
    CONSTRAINT codegen_llm_attempts_phase_role_check CHECK (
        (phase IN ('brief', 'review') AND role = 'helper')
        OR (phase IN ('edit', 'repair') AND role = 'editor')
    ),
    CONSTRAINT codegen_llm_attempts_credential_shape_check CHECK (
        (
            credential_id IS NOT NULL
            AND credential_version IS NOT NULL
            AND credential_version > 0
        )
        OR (
            status = 'blocked'
            AND credential_id IS NULL
            AND credential_version IS NULL
        )
    ),
    CONSTRAINT codegen_llm_attempts_lifecycle_check CHECK (
        (
            status = 'prepared'
            AND egress_at IS NULL
            AND finished_at IS NULL
            AND latency_ms IS NULL
            AND input_tokens IS NULL
            AND output_tokens IS NULL
            AND cost_usd_micros IS NULL
            AND error_classification IS NULL
        )
        OR (
            status = 'in_flight'
            AND egress_at IS NOT NULL
            AND finished_at IS NULL
            AND latency_ms IS NULL
            AND input_tokens IS NULL
            AND output_tokens IS NULL
            AND cost_usd_micros IS NULL
            AND error_classification IS NULL
            AND credential_id IS NOT NULL
        )
        OR (
            status = 'succeeded'
            AND egress_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND latency_ms IS NOT NULL
            AND error_classification IS NULL
            AND credential_id IS NOT NULL
        )
        OR (
            status = 'failed'
            AND egress_at IS NOT NULL
            AND finished_at IS NOT NULL
            AND latency_ms IS NOT NULL
            AND error_classification IS NOT NULL
            AND credential_id IS NOT NULL
        )
        OR (
            status = 'blocked'
            AND egress_at IS NULL
            AND finished_at IS NOT NULL
            AND latency_ms IS NULL
            AND input_tokens IS NULL
            AND output_tokens IS NULL
            AND cost_usd_micros IS NULL
            AND error_classification IS NOT NULL
        )
        OR (
            status = 'cancelled'
            AND finished_at IS NOT NULL
            AND error_classification = 'cancelled'
            AND (
                (
                    egress_at IS NULL
                    AND latency_ms IS NULL
                    AND input_tokens IS NULL
                    AND output_tokens IS NULL
                    AND cost_usd_micros IS NULL
                )
                OR (
                    egress_at IS NOT NULL
                    AND latency_ms IS NOT NULL
                    AND credential_id IS NOT NULL
                    AND credential_version IS NOT NULL
                )
            )
        )
    ),
    CONSTRAINT codegen_llm_attempts_usage_shape_check CHECK (
        (
            input_tokens IS NULL
            AND output_tokens IS NULL
            AND cost_usd_micros IS NULL
        )
        OR (
            input_tokens IS NOT NULL
            AND output_tokens IS NOT NULL
            AND cost_usd_micros IS NOT NULL
        )
    )
);

CREATE OR REPLACE FUNCTION apdl_validate_codegen_llm_attempt_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    snapshot_assignment JSONB;
BEGIN
    SELECT CASE NEW.role
        WHEN 'editor' THEN changeset.llm_execution_snapshot->'assignments'->0
        WHEN 'helper' THEN changeset.llm_execution_snapshot->'assignments'->1
        ELSE NULL
    END
    INTO snapshot_assignment
    FROM codegen_changesets AS changeset
    WHERE changeset.changeset_id = NEW.changeset_id
      AND changeset.project_id = NEW.project_id;

    IF snapshot_assignment IS NULL
       OR snapshot_assignment->>'role' <> NEW.role
       OR snapshot_assignment->>'provider' <> NEW.provider
       OR snapshot_assignment->>'model_id' <> NEW.model_id
       OR snapshot_assignment->>'assignment_version'
            <> NEW.assignment_version::TEXT THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Codegen LLM attempt must match its immutable execution snapshot';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER codegen_llm_attempts_validate_snapshot
BEFORE INSERT OR UPDATE OF
    project_id, changeset_id, role, provider, model_id, assignment_version
ON codegen_llm_attempts
FOR EACH ROW
EXECUTE FUNCTION apdl_validate_codegen_llm_attempt_snapshot();

SELECT apdl_register_execution_table(
    'public.codegen_llm_attempts'::REGCLASS
);

-- A blocked row records why an already-admitted changeset could not execute;
-- it is not execution authority itself. Keep the registry's authority check
-- for every inserted executable/effect-bearing state and every transition
-- into prepared/in_flight, while permitting the app to durably audit a
-- pre-egress authority loss. The composite changeset FK above still binds the
-- audit to the exact changeset and project.
CREATE OR REPLACE FUNCTION apdl_enforce_codegen_llm_attempt_project()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status = 'blocked' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' OR NEW.status IN ('prepared', 'in_flight') THEN
        PERFORM apdl_assert_execution_project_authorized(
            NEW.project_id,
            TG_TABLE_NAME
        );
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER apdl_execution_project_authorized ON codegen_llm_attempts;
CREATE TRIGGER apdl_execution_project_authorized
BEFORE INSERT OR UPDATE OF project_id, status ON codegen_llm_attempts
FOR EACH ROW
EXECUTE FUNCTION apdl_enforce_codegen_llm_attempt_project();

CREATE INDEX codegen_llm_attempts_changeset_idx
    ON codegen_llm_attempts (
        project_id, changeset_id, created_at, attempt_id
    );

CREATE OR REPLACE FUNCTION apdl_protect_codegen_llm_attempt_identity()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF (
        NEW.project_id,
        NEW.changeset_id,
        NEW.phase,
        NEW.role,
        NEW.attempt_sequence,
        NEW.provider,
        NEW.model_id,
        NEW.assignment_version,
        NEW.credential_id,
        NEW.credential_version
    ) IS DISTINCT FROM (
        OLD.project_id,
        OLD.changeset_id,
        OLD.phase,
        OLD.role,
        OLD.attempt_sequence,
        OLD.provider,
        OLD.model_id,
        OLD.assignment_version,
        OLD.credential_id,
        OLD.credential_version
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Codegen LLM attempt identity is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER codegen_llm_attempts_protect_identity
BEFORE UPDATE ON codegen_llm_attempts
FOR EACH ROW
EXECUTE FUNCTION apdl_protect_codegen_llm_attempt_identity();

CREATE OR REPLACE FUNCTION apdl_enforce_codegen_llm_attempt_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'prepared'
       AND NEW.status NOT IN ('in_flight', 'blocked', 'cancelled') THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Invalid transition from prepared Codegen LLM attempt';
    END IF;
    IF OLD.status = 'in_flight'
       AND NEW.status NOT IN ('succeeded', 'failed', 'cancelled') THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Invalid transition from in-flight Codegen LLM attempt';
    END IF;
    IF OLD.status IN ('succeeded', 'failed', 'blocked', 'cancelled') THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Terminal Codegen LLM attempts are immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER codegen_llm_attempts_enforce_transition
BEFORE UPDATE ON codegen_llm_attempts
FOR EACH ROW
EXECUTE FUNCTION apdl_enforce_codegen_llm_attempt_transition();
