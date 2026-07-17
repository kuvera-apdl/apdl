-- Migration 018: immutable, predeclared experiment statistics.
--
-- Existing experiments are deliberately left NULL. A plan authored after an
-- experiment observed traffic is not predeclared evidence, so Config's
-- analysis projection fails those legacy rows closed instead of backfilling a
-- misleading authoritative plan.

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS statistical_plan JSONB;

CREATE OR REPLACE FUNCTION public.apdl_experiment_statistical_plan_is_canonical(
    value JSONB
)
RETURNS BOOLEAN
LANGUAGE plpgsql
IMMUTABLE
AS $apdl_experiment_statistical_plan_is_canonical$
DECLARE
    baseline NUMERIC;
    effect NUMERIC;
    alpha NUMERIC;
    nominal NUMERIC;
    required_per_arm NUMERIC;
    settlement_seconds NUMERIC;
BEGIN
    IF jsonb_typeof(value) IS DISTINCT FROM 'object'
       OR (value
            - 'protocol'
            - 'baseline_conversion_rate'
            - 'minimum_detectable_effect'
            - 'significance_level'
            - 'nominal_power'
            - 'required_sample_size_per_arm'
            - 'data_settlement_seconds') <> '{}'::JSONB
       OR NOT value ?& ARRAY[
            'protocol',
            'baseline_conversion_rate',
            'minimum_detectable_effect',
            'significance_level',
            'nominal_power',
            'required_sample_size_per_arm',
            'data_settlement_seconds'
       ] THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(value->'protocol') IS DISTINCT FROM 'string'
       OR value->>'protocol' <> 'fixed_horizon_fisher_newcombe_cc_plan_v1' THEN
        RETURN false;
    END IF;
    IF jsonb_typeof(value->'baseline_conversion_rate') IS DISTINCT FROM 'number'
       OR jsonb_typeof(value->'minimum_detectable_effect') IS DISTINCT FROM 'number'
       OR jsonb_typeof(value->'significance_level') IS DISTINCT FROM 'number'
       OR jsonb_typeof(value->'nominal_power') IS DISTINCT FROM 'number'
       OR jsonb_typeof(value->'required_sample_size_per_arm') IS DISTINCT FROM 'number'
       OR jsonb_typeof(value->'data_settlement_seconds') IS DISTINCT FROM 'number' THEN
        RETURN false;
    END IF;

    baseline := (value->>'baseline_conversion_rate')::NUMERIC;
    effect := (value->>'minimum_detectable_effect')::NUMERIC;
    alpha := (value->>'significance_level')::NUMERIC;
    nominal := (value->>'nominal_power')::NUMERIC;
    required_per_arm := (value->>'required_sample_size_per_arm')::NUMERIC;
    settlement_seconds := (value->>'data_settlement_seconds')::NUMERIC;
    RETURN baseline BETWEEN 0 AND 1
       AND effect BETWEEN 0.000001 AND 1
       AND alpha BETWEEN 0.000001 AND 0.5
       AND nominal > 0.5 AND nominal <= 0.9999
       AND required_per_arm BETWEEN 2 AND 10000000
       AND required_per_arm = trunc(required_per_arm)
       AND settlement_seconds BETWEEN 1 AND 86400
       AND settlement_seconds = trunc(settlement_seconds);
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN false;
END
$apdl_experiment_statistical_plan_is_canonical$;

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_statistical_plan_object_check;
ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_statistical_plan_canonical_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_statistical_plan_canonical_check CHECK (
        statistical_plan IS NULL
        OR public.apdl_experiment_statistical_plan_is_canonical(statistical_plan)
    ) NOT VALID;

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_active_statistical_plan_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_active_statistical_plan_check CHECK (
        status NOT IN ('scheduled', 'running')
        OR public.apdl_experiment_statistical_plan_is_canonical(statistical_plan)
    ) NOT VALID;

CREATE OR REPLACE FUNCTION public.apdl_enforce_experiment_statistical_plan()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_experiment_statistical_plan$
BEGIN
    IF TG_OP = 'UPDATE'
       AND OLD.status <> 'draft'
       AND NEW.statistical_plan IS DISTINCT FROM OLD.statistical_plan THEN
        RAISE EXCEPTION 'experiment statistical_plan is immutable after draft';
    END IF;
    IF NEW.status IN ('scheduled', 'running')
       AND public.apdl_experiment_statistical_plan_is_canonical(
            NEW.statistical_plan
       ) IS NOT TRUE THEN
        RAISE EXCEPTION 'scheduled/running experiment requires canonical statistical_plan';
    END IF;
    RETURN NEW;
END
$apdl_enforce_experiment_statistical_plan$;

DROP TRIGGER IF EXISTS experiments_enforce_statistical_plan ON experiments;
CREATE TRIGGER experiments_enforce_statistical_plan
BEFORE INSERT OR UPDATE ON experiments
FOR EACH ROW EXECUTE FUNCTION public.apdl_enforce_experiment_statistical_plan();

-- Ship verdicts produced before this protocol have no trustworthy provenance.
-- Quarantine them so no future worker can reinterpret old significance as
-- deployment readiness.
UPDATE experiment_verdicts
SET consumed = TRUE
WHERE verdict = 'ship' AND consumed = FALSE;

COMMENT ON COLUMN experiments.statistical_plan IS
    'Immutable fixed-horizon nominal plan; NULL legacy rows cannot enter traffic';
