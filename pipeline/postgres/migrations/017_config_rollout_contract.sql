-- Migration 017: Canonical rollout values and fail-closed repair.
--
-- Rollout percentages live inside JSONB, so the database previously admitted
-- strings and other shapes that the evaluator could not safely consume. This
-- migration repairs those rows to a disabled zero-rollout configuration,
-- records the full before/after state, and installs the same strict contract
-- enforced by the Config request and mutation models.

ALTER TABLE flag_audit_log
    DROP CONSTRAINT IF EXISTS flag_audit_log_origin_check;
ALTER TABLE flag_audit_log
    ADD CONSTRAINT flag_audit_log_origin_check CHECK (
        origin IN ('manual', 'automation', 'experiment', 'scheduler', 'migration')
    );

CREATE OR REPLACE FUNCTION public.apdl_rollout_is_canonical(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_rollout_is_canonical$
DECLARE
    percentage NUMERIC;
BEGIN
    IF jsonb_typeof(value) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF NOT (value ? 'percentage')
       OR NOT (value ? 'bucket_by')
       OR (value - 'percentage' - 'bucket_by') <> '{}'::JSONB THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(value->'percentage') IS DISTINCT FROM 'number' THEN
        RETURN false;
    END IF;

    percentage := (value->>'percentage')::NUMERIC;
    IF percentage < 0 OR percentage > 100 THEN
        RETURN false;
    END IF;

    IF jsonb_typeof(value->'bucket_by') IS DISTINCT FROM 'string' THEN
        RETURN false;
    END IF;
    RETURN char_length(value->>'bucket_by') BETWEEN 1 AND 128;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN false;
END
$apdl_rollout_is_canonical$;

CREATE OR REPLACE FUNCTION public.apdl_rules_rollouts_are_canonical(value JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_rules_rollouts_are_canonical$
DECLARE
    rule JSONB;
BEGIN
    IF jsonb_typeof(value) IS DISTINCT FROM 'array'
       OR jsonb_array_length(value) > 50 THEN
        RETURN false;
    END IF;

    FOR rule IN SELECT item FROM jsonb_array_elements(value) AS items(item)
    LOOP
        IF jsonb_typeof(rule) IS DISTINCT FROM 'object'
           OR NOT (rule ? 'rollout')
           OR public.apdl_rollout_is_canonical(rule->'rollout') IS NOT TRUE THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
EXCEPTION
    WHEN invalid_parameter_value THEN
        RETURN false;
END
$apdl_rules_rollouts_are_canonical$;

CREATE OR REPLACE FUNCTION public.apdl_flag_rollouts_are_canonical(
    rules_value JSONB,
    fallthrough_value JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_flag_rollouts_are_canonical$
BEGIN
    IF public.apdl_rules_rollouts_are_canonical(rules_value) IS NOT TRUE THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(fallthrough_value) IS DISTINCT FROM 'object'
       OR NOT (fallthrough_value ? 'rollout')
       OR (fallthrough_value - 'rollout') <> '{}'::JSONB THEN
        RETURN false;
    END IF;
    RETURN public.apdl_rollout_is_canonical(
        fallthrough_value->'rollout'
    ) IS TRUE;
EXCEPTION
    WHEN invalid_parameter_value THEN
        RETURN false;
END
$apdl_flag_rollouts_are_canonical$;

CREATE OR REPLACE FUNCTION public.apdl_experiment_rules_are_canonical(value TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_experiment_rules_are_canonical$
DECLARE
    parsed JSONB;
BEGIN
    parsed := value::JSONB;
    RETURN public.apdl_rules_rollouts_are_canonical(parsed) IS TRUE;
EXCEPTION
    WHEN invalid_text_representation THEN
        RETURN false;
END
$apdl_experiment_rules_are_canonical$;

-- Repair corrupt experiment source fields first. Otherwise a later experiment
-- edit could regenerate an invalid backing flag after the flag constraint is
-- installed. Every repaired experiment and backing flag share one project
-- version, just like the normal atomic experiment mutation path.
DO $repair_invalid_experiment_rollouts$
DECLARE
    invalid_experiment RECORD;
    before_flag flags%ROWTYPE;
    repaired_flag flags%ROWTYPE;
    repaired_experiment experiments%ROWTYPE;
    before_snapshot JSONB;
    after_snapshot JSONB;
    next_project_version BIGINT;
    delivery_data JSONB;
BEGIN
    FOR invalid_experiment IN
        SELECT
            experiment.*,
            (
                experiment.traffic_percentage::TEXT IN (
                    'NaN', 'Infinity', '-Infinity'
                )
                OR experiment.traffic_percentage < 0
                OR experiment.traffic_percentage > 100
            ) AS invalid_traffic,
            (
                public.apdl_experiment_rules_are_canonical(
                    experiment.targeting_rules_json
                ) IS NOT TRUE
            ) AS invalid_rules
        FROM experiments AS experiment
        WHERE experiment.traffic_percentage::TEXT IN (
                  'NaN', 'Infinity', '-Infinity'
              )
           OR experiment.traffic_percentage < 0
           OR experiment.traffic_percentage > 100
           OR public.apdl_experiment_rules_are_canonical(
                  experiment.targeting_rules_json
              ) IS NOT TRUE
        ORDER BY experiment.project_id, experiment.key
        FOR UPDATE OF experiment
    LOOP
        SELECT flag.*
        INTO STRICT before_flag
        FROM flags AS flag
        WHERE flag.project_id = invalid_experiment.project_id
          AND flag.key = invalid_experiment.flag_key
        FOR UPDATE;

        before_snapshot := to_jsonb(before_flag);

        UPDATE experiments AS experiment
        SET traffic_percentage = CASE
                WHEN invalid_experiment.invalid_traffic THEN 0.0
                ELSE experiment.traffic_percentage
            END,
            targeting_rules_json = CASE
                WHEN invalid_experiment.invalid_rules THEN '[]'
                ELSE experiment.targeting_rules_json
            END,
            status = CASE
                WHEN experiment.status IN ('scheduled', 'running') THEN 'stopped'
                ELSE experiment.status
            END,
            end_date = CASE
                WHEN experiment.status = 'scheduled' THEN NULL
                WHEN experiment.status = 'running' THEN LEAST(
                    experiment.end_date,
                    now()
                )
                ELSE experiment.end_date
            END,
            version = experiment.version + 1,
            updated_at = now()
        WHERE experiment.project_id = invalid_experiment.project_id
          AND experiment.key = invalid_experiment.key
        RETURNING experiment.* INTO STRICT repaired_experiment;

        UPDATE flags AS flag
        SET state = CASE
                WHEN flag.state = 'archived' OR flag.archived_at IS NOT NULL
                    THEN 'archived'
                ELSE 'disabled'
            END,
            enabled = false,
            rules = '[]'::JSONB,
            fallthrough = jsonb_build_object(
                'rollout',
                jsonb_build_object('percentage', 0.0, 'bucket_by', 'user_id')
            ),
            disabled_reason = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_reason
                ELSE 'invalid_rollout_configuration'
            END,
            disabled_by = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_by
                ELSE 'system:migration:017'
            END,
            disabled_at = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_at
                ELSE COALESCE(flag.disabled_at, now())
            END,
            version = flag.version + 1,
            updated_at = now()
        WHERE flag.project_id = before_flag.project_id
          AND flag.key = before_flag.key
        RETURNING flag.* INTO STRICT repaired_flag;

        after_snapshot := to_jsonb(repaired_flag);
        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, origin,
            previous_version, new_version, before, after, evidence, reason
        ) VALUES (
            repaired_flag.project_id,
            repaired_flag.key,
            'flag_invalid_config_repaired',
            'system:migration:017',
            'migration',
            before_flag.version,
            repaired_flag.version,
            before_snapshot,
            after_snapshot,
            jsonb_build_object(
                'migration', '017_config_rollout_contract.sql',
                'experiment_key', repaired_experiment.key
            ),
            'invalid_rollout_configuration'
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
                '%s:%s:invalid_config_repaired',
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
                '%s:%s:invalid_config_repaired',
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
$repair_invalid_experiment_rollouts$;

-- Repair any remaining standalone or independently corrupted flag. No invalid
-- value is guessed or coerced; the only safe migration is a disabled zero
-- rollout with the original record retained in the audit log.
DO $repair_invalid_flag_rollouts$
DECLARE
    invalid_flag flags%ROWTYPE;
    repaired_flag flags%ROWTYPE;
    before_snapshot JSONB;
    after_snapshot JSONB;
    next_project_version BIGINT;
    delivery_data JSONB;
BEGIN
    FOR invalid_flag IN
        SELECT flag.*
        FROM flags AS flag
        WHERE public.apdl_flag_rollouts_are_canonical(
            flag.rules,
            flag.fallthrough
        ) IS NOT TRUE
        ORDER BY flag.project_id, flag.key
        FOR UPDATE
    LOOP
        before_snapshot := to_jsonb(invalid_flag);

        UPDATE flags AS flag
        SET state = CASE
                WHEN flag.state = 'archived' OR flag.archived_at IS NOT NULL
                    THEN 'archived'
                ELSE 'disabled'
            END,
            enabled = false,
            rules = '[]'::JSONB,
            fallthrough = jsonb_build_object(
                'rollout',
                jsonb_build_object('percentage', 0.0, 'bucket_by', 'user_id')
            ),
            disabled_reason = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_reason
                ELSE 'invalid_rollout_configuration'
            END,
            disabled_by = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_by
                ELSE 'system:migration:017'
            END,
            disabled_at = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_at
                ELSE COALESCE(flag.disabled_at, now())
            END,
            version = flag.version + 1,
            updated_at = now()
        WHERE flag.project_id = invalid_flag.project_id
          AND flag.key = invalid_flag.key
        RETURNING flag.* INTO STRICT repaired_flag;

        after_snapshot := to_jsonb(repaired_flag);
        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, origin,
            previous_version, new_version, before, after, evidence, reason
        ) VALUES (
            repaired_flag.project_id,
            repaired_flag.key,
            'flag_invalid_config_repaired',
            'system:migration:017',
            'migration',
            invalid_flag.version,
            repaired_flag.version,
            before_snapshot,
            after_snapshot,
            jsonb_build_object(
                'migration', '017_config_rollout_contract.sql'
            ),
            'invalid_rollout_configuration'
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
                '%s:%s:invalid_config_repaired',
                repaired_flag.key,
                repaired_flag.version
            ),
            jsonb_build_object(
                'event_type', 'flag_update',
                'project_version', next_project_version,
                'data', delivery_data
            )
        );
    END LOOP;
END
$repair_invalid_flag_rollouts$;

ALTER TABLE flags
    DROP CONSTRAINT IF EXISTS flags_rollouts_canonical_check;
ALTER TABLE flags
    ADD CONSTRAINT flags_rollouts_canonical_check CHECK (
        public.apdl_flag_rollouts_are_canonical(rules, fallthrough)
    );

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_traffic_percentage_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_traffic_percentage_check CHECK (
        traffic_percentage::TEXT NOT IN ('NaN', 'Infinity', '-Infinity')
        AND traffic_percentage >= 0.0
        AND traffic_percentage <= 100.0
    );

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_targeting_rollouts_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_targeting_rollouts_check CHECK (
        public.apdl_experiment_rules_are_canonical(targeting_rules_json)
    );
