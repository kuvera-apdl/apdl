-- Migration 042: exact cross-runtime weighted-variant bounds.
--
-- JavaScript can represent and add integers exactly only through 2^53 - 1.
-- Every flag and experiment therefore has at most ten variants, each weight
-- and the total are bounded by 9007199254740991, and the total is positive.
-- Invalid stored authority is never kept active: experiment bundles are
-- stopped/disabled and repaired to explicit safe control/treatment variants,
-- with both audit ledgers, row versions, project version, and outbox intents
-- advanced atomically. Standalone flags receive the same audited fail-closed
-- repair. The migration transaction aborts if an invalid experiment has no
-- backing flag to disable.

CREATE OR REPLACE FUNCTION public.apdl_flag_variants_are_canonical(
    variants_value JSONB,
    default_key TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public
AS $apdl_flag_variants_are_canonical$
DECLARE
    variant JSONB;
    variant_key TEXT;
    variant_weight NUMERIC;
    total_weight NUMERIC := 0;
    observed_keys TEXT[] := ARRAY[]::TEXT[];
    default_observed BOOLEAN := false;
BEGIN
    IF jsonb_typeof(variants_value) IS DISTINCT FROM 'array'
       OR jsonb_array_length(variants_value) < 1
       OR jsonb_array_length(variants_value) > 10 THEN
        RETURN false;
    END IF;

    FOR variant IN
        SELECT item
        FROM jsonb_array_elements(variants_value) AS items(item)
    LOOP
        IF jsonb_typeof(variant) IS DISTINCT FROM 'object'
           OR NOT (variant ? 'key')
           OR NOT (variant ? 'weight')
           OR (variant - 'key' - 'weight') <> '{}'::JSONB
           OR jsonb_typeof(variant->'key') IS DISTINCT FROM 'string'
           OR char_length(variant->>'key') NOT BETWEEN 1 AND 128
           OR jsonb_typeof(variant->'weight') IS DISTINCT FROM 'number'
           OR (variant->>'weight') !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN false;
        END IF;

        variant_key := variant->>'key';
        IF variant_key = ANY(observed_keys) THEN
            RETURN false;
        END IF;
        observed_keys := array_append(observed_keys, variant_key);
        default_observed := default_observed OR variant_key = default_key;

        variant_weight := (variant->>'weight')::NUMERIC;
        IF variant_weight > 9007199254740991 THEN
            RETURN false;
        END IF;
        total_weight := total_weight + variant_weight;
        IF total_weight > 9007199254740991 THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN total_weight > 0 AND default_observed;
EXCEPTION
    WHEN invalid_parameter_value
       OR invalid_text_representation
       OR numeric_value_out_of_range THEN
        RETURN false;
END
$apdl_flag_variants_are_canonical$;

CREATE OR REPLACE FUNCTION public.apdl_experiment_variants_are_canonical(
    variants_value TEXT,
    default_key TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public
AS $apdl_experiment_variants_are_canonical$
DECLARE
    parsed JSONB;
    variant JSONB;
    variant_key TEXT;
    variant_weight NUMERIC;
    total_weight NUMERIC := 0;
    observed_keys TEXT[] := ARRAY[]::TEXT[];
    default_observed BOOLEAN := false;
BEGIN
    parsed := variants_value::JSONB;
    IF jsonb_typeof(parsed) IS DISTINCT FROM 'array'
       OR jsonb_array_length(parsed) < 2
       OR jsonb_array_length(parsed) > 10 THEN
        RETURN false;
    END IF;

    FOR variant IN
        SELECT item
        FROM jsonb_array_elements(parsed) AS items(item)
    LOOP
        IF jsonb_typeof(variant) IS DISTINCT FROM 'object'
           OR NOT (variant ? 'key')
           OR NOT (variant ? 'weight')
           OR (variant - 'key' - 'weight' - 'description') <> '{}'::JSONB
           OR jsonb_typeof(variant->'key') IS DISTINCT FROM 'string'
           OR char_length(variant->>'key') NOT BETWEEN 1 AND 128
           OR jsonb_typeof(variant->'weight') IS DISTINCT FROM 'number'
           OR (variant->>'weight') !~ '^[1-9][0-9]*$'
           OR (
               variant ? 'description'
               AND jsonb_typeof(variant->'description')
                   IS DISTINCT FROM 'string'
           ) THEN
            RETURN false;
        END IF;

        variant_key := variant->>'key';
        IF variant_key = ANY(observed_keys) THEN
            RETURN false;
        END IF;
        observed_keys := array_append(observed_keys, variant_key);
        default_observed := default_observed OR variant_key = default_key;

        variant_weight := (variant->>'weight')::NUMERIC;
        IF variant_weight > 9007199254740991 THEN
            RETURN false;
        END IF;
        total_weight := total_weight + variant_weight;
        IF total_weight > 9007199254740991 THEN
            RETURN false;
        END IF;
    END LOOP;

    RETURN total_weight > 0 AND default_observed;
EXCEPTION
    WHEN invalid_parameter_value
       OR invalid_text_representation
       OR numeric_value_out_of_range THEN
        RETURN false;
END
$apdl_experiment_variants_are_canonical$;

DO $require_repairable_invalid_experiments$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM experiments AS experiment
        WHERE public.apdl_experiment_variants_are_canonical(
                  experiment.variants_json,
                  experiment.default_variant
              ) IS NOT TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM flags AS flag
              WHERE flag.project_id = experiment.project_id
                AND flag.key = experiment.flag_key
          )
    ) THEN
        RAISE EXCEPTION
            'Cannot repair invalid experiment variants without backing flag authority';
    END IF;
END
$require_repairable_invalid_experiments$;

-- The controlled repair can rewrite launched and archived experiment rows.
-- Drop only the guards that reject such rewrites; the migration transaction
-- restores them before commit and rolls the drops back on any failure.
DROP TRIGGER IF EXISTS experiments_enforce_enrollment_immutability
    ON experiments;
DROP TRIGGER IF EXISTS experiments_enforce_archive_lifecycle
    ON experiments;

DO $repair_invalid_experiment_variant_bundles$
DECLARE
    invalid_experiment RECORD;
    before_flag flags%ROWTYPE;
    repaired_flag flags%ROWTYPE;
    repaired_experiment experiments%ROWTYPE;
    canonical_experiment_variants JSONB;
    canonical_flag_variants JSONB;
    canonical_default_variant TEXT;
    next_project_version BIGINT;
    delivery_data JSONB;
BEGIN
    FOR invalid_experiment IN
        SELECT
            experiment.*,
            public.apdl_experiment_variants_are_canonical(
                experiment.variants_json,
                experiment.default_variant
            ) AS experiment_variants_valid
        FROM experiments AS experiment
        WHERE public.apdl_experiment_variants_are_canonical(
                  experiment.variants_json,
                  experiment.default_variant
              ) IS NOT TRUE
           OR EXISTS (
               SELECT 1
               FROM flags AS flag
               WHERE flag.project_id = experiment.project_id
                 AND flag.key = experiment.flag_key
                 AND public.apdl_flag_variants_are_canonical(
                         flag.variants,
                         flag.default_variant
                     ) IS NOT TRUE
           )
        ORDER BY experiment.project_id, experiment.key
        FOR UPDATE OF experiment
    LOOP
        SELECT flag.*
        INTO STRICT before_flag
        FROM flags AS flag
        WHERE flag.project_id = invalid_experiment.project_id
          AND flag.key = invalid_experiment.flag_key
        FOR UPDATE;

        IF invalid_experiment.experiment_variants_valid IS TRUE THEN
            canonical_experiment_variants :=
                invalid_experiment.variants_json::JSONB;
            canonical_default_variant :=
                invalid_experiment.default_variant;
        ELSE
            canonical_experiment_variants := jsonb_build_array(
                jsonb_build_object(
                    'key', 'control',
                    'weight', 1,
                    'description', ''
                ),
                jsonb_build_object(
                    'key', 'treatment',
                    'weight', 1,
                    'description', ''
                )
            );
            canonical_default_variant := 'control';
        END IF;

        SELECT jsonb_agg(
            jsonb_build_object(
                'key', variant->'key',
                'weight', variant->'weight'
            )
            ORDER BY position
        )
        INTO STRICT canonical_flag_variants
        FROM jsonb_array_elements(canonical_experiment_variants)
            WITH ORDINALITY AS variants(variant, position);

        UPDATE experiments AS experiment
        SET variants_json = CASE
                WHEN invalid_experiment.experiment_variants_valid IS TRUE
                    THEN experiment.variants_json
                ELSE canonical_experiment_variants::TEXT
            END,
            default_variant = canonical_default_variant,
            status = CASE
                WHEN experiment.status IN ('scheduled', 'running')
                    THEN 'stopped'
                ELSE experiment.status
            END,
            end_date = CASE
                WHEN experiment.status = 'scheduled' THEN NULL
                WHEN experiment.status = 'running' THEN LEAST(
                    COALESCE(experiment.end_date, now()),
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
            default_variant = canonical_default_variant,
            variants = canonical_flag_variants,
            disabled_reason = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_reason
                ELSE 'invalid_variant_configuration'
            END,
            disabled_by = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_by
                ELSE 'system:migration:042'
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

        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, origin,
            previous_version, new_version, before, after, evidence, reason
        ) VALUES (
            repaired_flag.project_id,
            repaired_flag.key,
            'flag_invalid_config_repaired',
            'system:migration:042',
            'migration',
            before_flag.version,
            repaired_flag.version,
            to_jsonb(before_flag),
            to_jsonb(repaired_flag),
            jsonb_build_object(
                'migration', '042_variant_weight_contract.sql',
                'experiment_key', repaired_experiment.key
            ),
            'invalid_variant_configuration'
        );

        INSERT INTO experiment_audit_log (
            project_id, experiment_key, action, actor,
            previous_version, new_version, before, after
        ) VALUES (
            repaired_experiment.project_id,
            repaired_experiment.key,
            'experiment_updated',
            'system:migration:042',
            invalid_experiment.version,
            repaired_experiment.version,
            to_jsonb(invalid_experiment)
                - 'experiment_variants_valid',
            to_jsonb(repaired_experiment)
        );

        INSERT INTO config_project_versions (project_id, project_version)
        VALUES (repaired_flag.project_id, 1)
        ON CONFLICT (project_id) DO UPDATE
        SET project_version =
                config_project_versions.project_version + 1,
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
                        'default_variant',
                            repaired_flag.default_variant,
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

        INSERT INTO config_outbox (
            project_id, kind, dedup_key, payload
        ) VALUES (
            repaired_flag.project_id,
            'flag_change',
            format(
                '%s:%s:variant_contract_repaired',
                repaired_flag.key,
                repaired_flag.version
            ),
            jsonb_build_object(
                'event_type', 'flag_update',
                'project_version', next_project_version,
                'data', delivery_data
            )
        );

        INSERT INTO config_outbox (
            project_id, kind, dedup_key, payload
        ) VALUES (
            repaired_experiment.project_id,
            'experiment_change',
            format(
                '%s:%s:variant_contract_repaired',
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
$repair_invalid_experiment_variant_bundles$;

-- Any remaining invalid flag is standalone. Replace its unsafe allocation with
-- explicit safe variants, make it non-serving, and publish the new version.
DO $repair_invalid_standalone_flag_variants$
DECLARE
    invalid_flag flags%ROWTYPE;
    repaired_flag flags%ROWTYPE;
    next_project_version BIGINT;
    delivery_data JSONB;
BEGIN
    FOR invalid_flag IN
        SELECT flag.*
        FROM flags AS flag
        WHERE public.apdl_flag_variants_are_canonical(
                  flag.variants,
                  flag.default_variant
              ) IS NOT TRUE
        ORDER BY flag.project_id, flag.key
        FOR UPDATE
    LOOP
        UPDATE flags AS flag
        SET state = CASE
                WHEN flag.state = 'archived' OR flag.archived_at IS NOT NULL
                    THEN 'archived'
                ELSE 'disabled'
            END,
            enabled = false,
            default_variant = 'control',
            variants = jsonb_build_array(
                jsonb_build_object('key', 'control', 'weight', 1),
                jsonb_build_object('key', 'treatment', 'weight', 1)
            ),
            disabled_reason = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_reason
                ELSE 'invalid_variant_configuration'
            END,
            disabled_by = CASE
                WHEN flag.state = 'archived' THEN flag.disabled_by
                ELSE 'system:migration:042'
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

        INSERT INTO flag_audit_log (
            project_id, flag_key, action, actor, origin,
            previous_version, new_version, before, after, evidence, reason
        ) VALUES (
            repaired_flag.project_id,
            repaired_flag.key,
            'flag_invalid_config_repaired',
            'system:migration:042',
            'migration',
            invalid_flag.version,
            repaired_flag.version,
            to_jsonb(invalid_flag),
            to_jsonb(repaired_flag),
            jsonb_build_object(
                'migration', '042_variant_weight_contract.sql'
            ),
            'invalid_variant_configuration'
        );

        INSERT INTO config_project_versions (project_id, project_version)
        VALUES (repaired_flag.project_id, 1)
        ON CONFLICT (project_id) DO UPDATE
        SET project_version =
                config_project_versions.project_version + 1,
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
                        'default_variant',
                            repaired_flag.default_variant,
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

        INSERT INTO config_outbox (
            project_id, kind, dedup_key, payload
        ) VALUES (
            repaired_flag.project_id,
            'flag_change',
            format(
                '%s:%s:variant_contract_repaired',
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
$repair_invalid_standalone_flag_variants$;

ALTER TABLE flags
    DROP CONSTRAINT IF EXISTS flags_variants_canonical_check;
ALTER TABLE flags
    ADD CONSTRAINT flags_variants_canonical_check CHECK (
        public.apdl_flag_variants_are_canonical(
            variants,
            default_variant
        )
    ) NOT VALID;
ALTER TABLE flags
    VALIDATE CONSTRAINT flags_variants_canonical_check;

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_variants_canonical_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_variants_canonical_check CHECK (
        public.apdl_experiment_variants_are_canonical(
            variants_json,
            default_variant
        )
    ) NOT VALID;
ALTER TABLE experiments
    VALIDATE CONSTRAINT experiments_variants_canonical_check;

CREATE TRIGGER experiments_enforce_enrollment_immutability
BEFORE UPDATE OF status, traffic_percentage, targeting_rules_json,
    minimum_exposure_config_version ON experiments
FOR EACH ROW EXECUTE FUNCTION
    public.apdl_enforce_experiment_enrollment_immutability();

CREATE TRIGGER experiments_enforce_archive_lifecycle
BEFORE UPDATE OR DELETE ON experiments
FOR EACH ROW EXECUTE FUNCTION
    public.apdl_enforce_experiment_archive_lifecycle();

COMMENT ON FUNCTION public.apdl_flag_variants_are_canonical(JSONB, TEXT) IS
    'Enforces at most ten unique variants with exact safe-integer weights and total';
COMMENT ON FUNCTION public.apdl_experiment_variants_are_canonical(TEXT, TEXT) IS
    'Enforces the authored experiment variant shape and exact safe-integer bounds';
