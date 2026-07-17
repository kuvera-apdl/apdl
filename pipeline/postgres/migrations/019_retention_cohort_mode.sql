-- Retention is canonically window-relative. Existing custom-agent presets
-- predate the required declaration and must be upgraded explicitly so they do
-- not fail validation after the query contract becomes strict.
--
-- Preserve array order and every unrelated field. Only a missing declaration
-- is migrated; an explicitly conflicting value remains visible and invalid
-- rather than being silently reinterpreted.
WITH migrated_presets AS (
    SELECT
        agent.agent_id,
        jsonb_agg(
            CASE
                WHEN entry.value ->> 'tool' = 'query_retention'
                     AND jsonb_typeof(entry.value -> 'params') = 'object'
                     AND NOT (entry.value -> 'params' ? 'cohort_mode')
                THEN jsonb_set(
                    entry.value,
                    '{params,cohort_mode}',
                    to_jsonb('first_match_in_window'::text),
                    true
                )
                ELSE entry.value
            END
            ORDER BY entry.ordinality
        ) AS preset_tools
    FROM custom_agents AS agent
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE
            WHEN jsonb_typeof(agent.preset_tools) = 'array'
            THEN agent.preset_tools
            ELSE '[]'::jsonb
        END
    )
        WITH ORDINALITY AS entry(value, ordinality)
    GROUP BY agent.agent_id
)
UPDATE custom_agents AS agent
SET preset_tools = migrated.preset_tools,
    updated_at = now()
FROM migrated_presets AS migrated
WHERE agent.agent_id = migrated.agent_id
  AND agent.preset_tools IS DISTINCT FROM migrated.preset_tools;
