-- Migration 035: one canonical experiment enrollment contract.
--
-- Experiment targeting rules express eligibility only.  The experiment's
-- traffic_percentage is the sole rollout authority.  Backing flags therefore
-- apply that percentage to each eligibility rule and use a zero fallthrough
-- whenever targeting is present.  A durable minimum flag version prevents
-- analysis from reinterpreting assignments produced by an incompatible legacy
-- projection.

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS minimum_exposure_config_version INTEGER;

CREATE OR REPLACE FUNCTION public.apdl_experiment_condition_is_canonical(
    condition JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_experiment_condition_is_canonical$
DECLARE
    operator_value TEXT;
    member JSONB;
BEGIN
    IF jsonb_typeof(condition) IS DISTINCT FROM 'object'
       OR NOT (condition ? 'attribute')
       OR NOT (condition ? 'operator')
       OR jsonb_typeof(condition->'attribute') IS DISTINCT FROM 'string'
       OR char_length(condition->>'attribute') NOT BETWEEN 1 AND 128
       OR jsonb_typeof(condition->'operator') IS DISTINCT FROM 'string' THEN
        RETURN false;
    END IF;

    operator_value := condition->>'operator';
    IF operator_value IN ('exists', 'not_exists') THEN
        RETURN (condition - 'attribute' - 'operator') = '{}'::JSONB;
    END IF;
    IF operator_value NOT IN (
        'equals', 'not_equals', 'gt', 'gte', 'lt', 'lte', 'contains',
        'not_contains', 'starts_with', 'ends_with', 'in', 'not_in'
    )
       OR NOT (condition ? 'value')
       OR (condition - 'attribute' - 'operator' - 'value') <> '{}'::JSONB THEN
        RETURN false;
    END IF;

    IF operator_value IN ('equals', 'not_equals') THEN
        RETURN jsonb_typeof(condition->'value') IN ('boolean', 'number')
            OR (
                jsonb_typeof(condition->'value') = 'string'
                AND char_length(condition->>'value') <= 256
            );
    END IF;
    IF operator_value IN (
        'contains', 'not_contains', 'starts_with', 'ends_with'
    ) THEN
        RETURN jsonb_typeof(condition->'value') = 'string'
            AND char_length(condition->>'value') <= 256;
    END IF;
    IF operator_value IN ('gt', 'gte', 'lt', 'lte') THEN
        IF jsonb_typeof(condition->'value') = 'number' THEN
            RETURN true;
        END IF;
        RETURN jsonb_typeof(condition->'value') = 'string'
            AND char_length(condition->>'value') <= 256
            AND condition->>'value' ~
                '^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$'
            AND (condition->>'value')::DOUBLE PRECISION NOT IN (
                'Infinity'::DOUBLE PRECISION,
                '-Infinity'::DOUBLE PRECISION
            );
    END IF;

    IF jsonb_typeof(condition->'value') IS DISTINCT FROM 'array'
       OR jsonb_array_length(condition->'value') NOT BETWEEN 1 AND 100 THEN
        RETURN false;
    END IF;
    FOR member IN
        SELECT value
        FROM jsonb_array_elements(condition->'value') AS members(value)
    LOOP
        IF jsonb_typeof(member) NOT IN ('boolean', 'number')
           AND NOT (
               jsonb_typeof(member) = 'string'
               AND char_length(member #>> '{}') <= 256
           ) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN false;
END
$apdl_experiment_condition_is_canonical$;

CREATE OR REPLACE FUNCTION public.apdl_legacy_experiment_rules_can_migrate(
    value TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_legacy_experiment_rules_can_migrate$
DECLARE
    parsed JSONB;
    rule JSONB;
    condition JSONB;
BEGIN
    parsed := value::JSONB;
    IF jsonb_typeof(parsed) IS DISTINCT FROM 'array'
       OR jsonb_array_length(parsed) > 50 THEN
        RETURN false;
    END IF;
    FOR rule IN SELECT item FROM jsonb_array_elements(parsed) AS rules(item)
    LOOP
        IF jsonb_typeof(rule) IS DISTINCT FROM 'object'
           OR NOT (rule ? 'id')
           OR NOT (rule ? 'conditions')
           OR NOT (rule ? 'rollout')
           OR (rule - 'id' - 'name' - 'conditions' - 'rollout') <> '{}'::JSONB
           OR jsonb_typeof(rule->'id') IS DISTINCT FROM 'string'
           OR char_length(rule->>'id') NOT BETWEEN 1 AND 128
           OR (
               rule ? 'name'
               AND (
                   jsonb_typeof(rule->'name') IS DISTINCT FROM 'string'
                   OR char_length(rule->>'name') > 256
               )
           )
           OR jsonb_typeof(rule->'conditions') IS DISTINCT FROM 'array'
           OR jsonb_array_length(rule->'conditions') > 20
           OR public.apdl_rollout_is_canonical(rule->'rollout') IS NOT TRUE THEN
            RETURN false;
        END IF;
        FOR condition IN
            SELECT item
            FROM jsonb_array_elements(rule->'conditions') AS conditions(item)
        LOOP
            IF public.apdl_experiment_condition_is_canonical(condition) IS NOT TRUE THEN
                RETURN false;
            END IF;
        END LOOP;
    END LOOP;
    RETURN true;
EXCEPTION
    WHEN invalid_text_representation OR invalid_parameter_value THEN
        RETURN false;
END
$apdl_legacy_experiment_rules_can_migrate$;

DO $validate_legacy_experiment_targeting$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE public.apdl_legacy_experiment_rules_can_migrate(
            targeting_rules_json
        ) IS NOT TRUE
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiment targeting: stored rules are not canonical';
    END IF;
END
$validate_legacy_experiment_targeting$;

-- The controlled rewrite below changes immutable launched rows and archived
-- tombstones.  Drop only the two guards that would reject this migration, then
-- restore stricter versions after every row and its audit/outbox intent has
-- been committed by the migration transaction.
DROP TRIGGER IF EXISTS experiments_enforce_enrollment_immutability
    ON experiments;
DROP TRIGGER IF EXISTS experiments_enforce_archive_lifecycle
    ON experiments;
ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_targeting_rollouts_check;

DO $repair_targeted_experiment_flags$
DECLARE
    legacy_experiment experiments%ROWTYPE;
    before_flag flags%ROWTYPE;
    repaired_flag flags%ROWTYPE;
    repaired_experiment experiments%ROWTYPE;
    canonical_targeting JSONB;
    projected_rules JSONB;
    history_is_compatible BOOLEAN;
    next_project_version BIGINT;
    delivery_data JSONB;
BEGIN
    FOR legacy_experiment IN
        SELECT experiment.*
        FROM experiments AS experiment
        WHERE jsonb_array_length(experiment.targeting_rules_json::JSONB) > 0
        ORDER BY experiment.project_id, experiment.key
        FOR UPDATE OF experiment
    LOOP
        SELECT flag.*
        INTO STRICT before_flag
        FROM flags AS flag
        WHERE flag.project_id = legacy_experiment.project_id
          AND flag.key = legacy_experiment.flag_key
        FOR UPDATE;

        SELECT COALESCE(
            jsonb_agg(
                jsonb_build_object(
                    'id', rule->'id',
                    'name', COALESCE(rule->'name', '""'::JSONB),
                    'conditions', rule->'conditions'
                )
                ORDER BY position
            ),
            '[]'::JSONB
        )
        INTO STRICT canonical_targeting
        FROM jsonb_array_elements(
            legacy_experiment.targeting_rules_json::JSONB
        ) WITH ORDINALITY AS rules(rule, position);

        SELECT COALESCE(
            bool_and(
                (rule->'rollout'->>'percentage')::NUMERIC
                    = legacy_experiment.traffic_percentage::NUMERIC
                AND rule->'rollout'->>'bucket_by' = 'user_id'
            ),
            true
        )
        INTO STRICT history_is_compatible
        FROM jsonb_array_elements(
            legacy_experiment.targeting_rules_json::JSONB
        ) AS rules(rule);

        SELECT COALESCE(
            jsonb_agg(
                rule || jsonb_build_object(
                    'rollout', jsonb_build_object(
                        'percentage', legacy_experiment.traffic_percentage,
                        'bucket_by', 'user_id'
                    )
                )
                ORDER BY position
            ),
            '[]'::JSONB
        )
        INTO STRICT projected_rules
        FROM jsonb_array_elements(canonical_targeting)
            WITH ORDINALITY AS rules(rule, position);

        UPDATE flags AS flag
        SET rules = projected_rules,
            fallthrough = jsonb_build_object(
                'rollout', jsonb_build_object(
                    'percentage', 0.0,
                    'bucket_by', 'user_id'
                )
            ),
            version = flag.version + 1,
            updated_at = now()
        WHERE flag.project_id = before_flag.project_id
          AND flag.key = before_flag.key
        RETURNING flag.* INTO STRICT repaired_flag;

        UPDATE experiments AS experiment
        SET targeting_rules_json = canonical_targeting::TEXT,
            minimum_exposure_config_version = CASE
                WHEN experiment.status = 'draft' THEN NULL
                WHEN history_is_compatible THEN 1
                ELSE repaired_flag.version
            END,
            version = experiment.version + 1,
            updated_at = now()
        WHERE experiment.project_id = legacy_experiment.project_id
          AND experiment.key = legacy_experiment.key
        RETURNING experiment.* INTO STRICT repaired_experiment;

        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, origin,
            previous_version, new_version, before, after, evidence, reason
        ) VALUES (
            repaired_flag.project_id,
            repaired_flag.key,
            'flag_updated',
            'system:migration:035',
            'migration',
            before_flag.version,
            repaired_flag.version,
            to_jsonb(before_flag),
            to_jsonb(repaired_flag),
            jsonb_build_object(
                'migration', '035_experiment_enrollment_contract.sql',
                'experiment_key', repaired_experiment.key,
                'history_compatible', history_is_compatible
            ),
            'canonical_experiment_enrollment_projection'
        );

        INSERT INTO experiment_audit_log (
            project_id, experiment_key, action, actor, previous_version,
            new_version, before, after
        ) VALUES (
            repaired_experiment.project_id,
            repaired_experiment.key,
            'experiment_updated',
            'system:migration:035',
            legacy_experiment.version,
            repaired_experiment.version,
            to_jsonb(legacy_experiment),
            to_jsonb(repaired_experiment)
        );

        INSERT INTO config_project_versions (project_id, project_version)
        VALUES (repaired_flag.project_id, 1)
        ON CONFLICT (project_id) DO UPDATE
        SET project_version = config_project_versions.project_version + 1,
            updated_at = now()
        RETURNING project_version INTO STRICT next_project_version;

        delivery_data := CASE
            WHEN repaired_flag.evaluation_mode IN ('client', 'both')
             AND repaired_flag.archived_at IS NULL THEN
                jsonb_build_object(
                    'action', 'flag_updated',
                    'flag', jsonb_build_object(
                        'key', repaired_flag.key,
                        'enabled', repaired_flag.enabled,
                        'default_variant', repaired_flag.default_variant,
                        'variants', repaired_flag.variants,
                        'salt', repaired_flag.salt,
                        'rules', repaired_flag.rules,
                        'fallthrough', repaired_flag.fallthrough,
                        'version', repaired_flag.version
                    ),
                    'version', repaired_flag.version
                )
            ELSE jsonb_build_object(
                'action', 'flag_removed',
                'key', repaired_flag.key,
                'version', repaired_flag.version
            )
        END;

        INSERT INTO config_outbox (project_id, kind, dedup_key, payload)
        VALUES (
            repaired_flag.project_id,
            'flag_change',
            format(
                '%s:%s:enrollment_contract_repaired',
                repaired_flag.key,
                repaired_flag.version
            ),
            jsonb_build_object(
                'event_type', 'flag_update',
                'project_version', next_project_version,
                'data', delivery_data
            )
        );

        INSERT INTO config_outbox (project_id, kind, dedup_key, payload)
        VALUES (
            repaired_experiment.project_id,
            'experiment_change',
            format(
                '%s:%s:enrollment_contract_repaired',
                repaired_experiment.key,
                repaired_experiment.version
            ),
            jsonb_build_object(
                'event_type', 'experiment_update',
                'project_version', next_project_version,
                'data', jsonb_build_object(
                    'action', 'experiment_updated',
                    'key', repaired_experiment.key,
                    'status', repaired_experiment.status,
                    'flag_key', repaired_experiment.flag_key,
                    'version', repaired_experiment.version
                )
            )
        );
    END LOOP;
END
$repair_targeted_experiment_flags$;

-- Untargeted backing flags already use the correct fallthrough projection, so
-- every historical config version is eligible for analysis.
DO $backfill_untargeted_experiment_versions$
DECLARE
    legacy_experiment experiments%ROWTYPE;
    repaired_experiment experiments%ROWTYPE;
    next_project_version BIGINT;
BEGIN
    FOR legacy_experiment IN
        SELECT experiment.*
        FROM experiments AS experiment
        WHERE experiment.status <> 'draft'
          AND jsonb_array_length(
              experiment.targeting_rules_json::JSONB
          ) = 0
          AND experiment.minimum_exposure_config_version IS NULL
        ORDER BY experiment.project_id, experiment.key
        FOR UPDATE OF experiment
    LOOP
        UPDATE experiments AS experiment
        SET minimum_exposure_config_version = 1,
            version = experiment.version + 1,
            updated_at = now()
        WHERE experiment.project_id = legacy_experiment.project_id
          AND experiment.key = legacy_experiment.key
        RETURNING experiment.* INTO STRICT repaired_experiment;

        INSERT INTO experiment_audit_log (
            project_id, experiment_key, action, actor, previous_version,
            new_version, before, after
        ) VALUES (
            repaired_experiment.project_id,
            repaired_experiment.key,
            'experiment_updated',
            'system:migration:035',
            legacy_experiment.version,
            repaired_experiment.version,
            to_jsonb(legacy_experiment),
            to_jsonb(repaired_experiment)
        );

        INSERT INTO config_project_versions (project_id, project_version)
        VALUES (repaired_experiment.project_id, 1)
        ON CONFLICT (project_id) DO UPDATE
        SET project_version = config_project_versions.project_version + 1,
            updated_at = now()
        RETURNING project_version INTO STRICT next_project_version;

        INSERT INTO config_outbox (project_id, kind, dedup_key, payload)
        VALUES (
            repaired_experiment.project_id,
            'experiment_change',
            format(
                '%s:%s:enrollment_contract_backfilled',
                repaired_experiment.key,
                repaired_experiment.version
            ),
            jsonb_build_object(
                'event_type', 'experiment_update',
                'project_version', next_project_version,
                'data', jsonb_build_object(
                    'action', 'experiment_updated',
                    'key', repaired_experiment.key,
                    'status', repaired_experiment.status,
                    'flag_key', repaired_experiment.flag_key,
                    'version', repaired_experiment.version
                )
            )
        );
    END LOOP;
END
$backfill_untargeted_experiment_versions$;

CREATE OR REPLACE FUNCTION public.apdl_experiment_rules_are_canonical(value TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_experiment_rules_are_canonical$
DECLARE
    parsed JSONB;
    rule JSONB;
    condition JSONB;
BEGIN
    parsed := value::JSONB;
    IF jsonb_typeof(parsed) IS DISTINCT FROM 'array'
       OR jsonb_array_length(parsed) > 50 THEN
        RETURN false;
    END IF;
    FOR rule IN SELECT item FROM jsonb_array_elements(parsed) AS rules(item)
    LOOP
        IF jsonb_typeof(rule) IS DISTINCT FROM 'object'
           OR NOT (rule ? 'id')
           OR NOT (rule ? 'name')
           OR NOT (rule ? 'conditions')
           OR (rule - 'id' - 'name' - 'conditions') <> '{}'::JSONB
           OR jsonb_typeof(rule->'id') IS DISTINCT FROM 'string'
           OR char_length(rule->>'id') NOT BETWEEN 1 AND 128
           OR jsonb_typeof(rule->'name') IS DISTINCT FROM 'string'
           OR char_length(rule->>'name') > 256
           OR jsonb_typeof(rule->'conditions') IS DISTINCT FROM 'array'
           OR jsonb_array_length(rule->'conditions') > 20 THEN
            RETURN false;
        END IF;
        FOR condition IN
            SELECT item
            FROM jsonb_array_elements(rule->'conditions') AS conditions(item)
        LOOP
            IF public.apdl_experiment_condition_is_canonical(condition) IS NOT TRUE THEN
                RETURN false;
            END IF;
        END LOOP;
    END LOOP;
    RETURN true;
EXCEPTION
    WHEN invalid_text_representation OR invalid_parameter_value THEN
        RETURN false;
END
$apdl_experiment_rules_are_canonical$;

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_targeting_rollouts_check;
ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_targeting_rules_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_targeting_rules_check CHECK (
        public.apdl_experiment_rules_are_canonical(targeting_rules_json)
    );

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_minimum_exposure_version_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_minimum_exposure_version_check CHECK (
        (
            status = 'draft'
            AND minimum_exposure_config_version IS NULL
        )
        OR (
            status <> 'draft'
            AND minimum_exposure_config_version >= 1
        )
    );

CREATE OR REPLACE FUNCTION public.apdl_enforce_experiment_enrollment_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_experiment_enrollment_immutability$
BEGIN
    IF OLD.status <> 'draft'
       AND (
           NEW.traffic_percentage IS DISTINCT FROM OLD.traffic_percentage
           OR NEW.targeting_rules_json IS DISTINCT FROM OLD.targeting_rules_json
           OR NEW.minimum_exposure_config_version IS DISTINCT FROM
              OLD.minimum_exposure_config_version
       ) THEN
        RAISE EXCEPTION
            'experiment enrollment is immutable after draft';
    END IF;
    RETURN NEW;
END
$apdl_enforce_experiment_enrollment_immutability$;

CREATE TRIGGER experiments_enforce_enrollment_immutability
BEFORE UPDATE OF status, traffic_percentage, targeting_rules_json,
    minimum_exposure_config_version ON experiments
FOR EACH ROW EXECUTE FUNCTION
    public.apdl_enforce_experiment_enrollment_immutability();

CREATE TRIGGER experiments_enforce_archive_lifecycle
BEFORE UPDATE OR DELETE ON experiments
FOR EACH ROW EXECUTE FUNCTION public.apdl_enforce_experiment_archive_lifecycle();

DROP FUNCTION public.apdl_legacy_experiment_rules_can_migrate(TEXT);

COMMENT ON COLUMN experiments.minimum_exposure_config_version IS
    'Lowest backing-flag version whose assignment exposure is valid for analysis';
COMMENT ON FUNCTION public.apdl_experiment_rules_are_canonical(TEXT) IS
    'Validates eligibility-only experiment targeting rules with no rollout field';
