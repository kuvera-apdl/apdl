-- Migration 016: Monotonic project versions for Config snapshots and delivery.
--
-- A flag collection needs one project-wide ordering token. Entity versions
-- cannot order changes to different flags, and therefore cannot reconcile an
-- SSE snapshot with concurrent updates or protect cache population races.

CREATE TABLE config_project_versions (
    project_id TEXT PRIMARY KEY,
    project_version BIGINT NOT NULL
        CHECK (project_version >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stamp existing durable Config deliveries in their committed order. This
-- makes a pending pre-upgrade outbox row valid for the new delivery contract
-- and establishes the starting version for each project.
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY project_id
            ORDER BY id
        )::BIGINT AS project_version
    FROM config_outbox
    WHERE kind IN ('flag_change', 'experiment_change')
)
UPDATE config_outbox AS outbox
SET payload = jsonb_set(
    outbox.payload - 'version',
    '{project_version}',
    to_jsonb(ranked.project_version),
    true
)
FROM ranked
WHERE outbox.id = ranked.id;

INSERT INTO config_project_versions (project_id, project_version)
SELECT
    project_id,
    max((payload->>'project_version')::BIGINT)
FROM config_outbox
WHERE kind IN ('flag_change', 'experiment_change')
GROUP BY project_id;

ALTER TABLE config_outbox
    ADD CONSTRAINT config_outbox_project_version_check CHECK (
        kind NOT IN ('flag_change', 'experiment_change')
        OR (
            payload ? 'project_version'
            AND jsonb_typeof(payload->'project_version') IS NOT DISTINCT FROM 'number'
            AND COALESCE(
                payload->>'project_version' ~ '^[1-9][0-9]*$',
                false
            )
        )
    );
