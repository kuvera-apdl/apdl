-- Migration 043: explicit stable experiment bucketing identity.
--
-- Experiments previously projected every backing flag with user_id. Preserve
-- that exact behavior for existing rows, then require new authoring to choose
-- one of the two supported actor identities explicitly. The column deliberately
-- has no default so every write path must carry the stored authority.

ALTER TABLE experiments
    ADD COLUMN IF NOT EXISTS bucket_by TEXT;

UPDATE experiments
SET bucket_by = 'user_id'
WHERE bucket_by IS NULL;

DO $validate_experiment_bucket_identity$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM experiments
        WHERE bucket_by NOT IN ('anonymous_id', 'user_id')
    ) THEN
        RAISE EXCEPTION
            'Cannot migrate experiments with an unsupported bucket_by identity';
    END IF;
END
$validate_experiment_bucket_identity$;

ALTER TABLE experiments
    ALTER COLUMN bucket_by DROP DEFAULT,
    ALTER COLUMN bucket_by SET NOT NULL;

ALTER TABLE experiments
    DROP CONSTRAINT IF EXISTS experiments_bucket_by_check;
ALTER TABLE experiments
    ADD CONSTRAINT experiments_bucket_by_check CHECK (
        bucket_by IN ('anonymous_id', 'user_id')
    ) NOT VALID;
ALTER TABLE experiments
    VALIDATE CONSTRAINT experiments_bucket_by_check;

CREATE OR REPLACE FUNCTION public.apdl_enforce_experiment_enrollment_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_experiment_enrollment_immutability$
BEGIN
    IF OLD.status <> 'draft'
       AND (
           NEW.bucket_by IS DISTINCT FROM OLD.bucket_by
           OR NEW.traffic_percentage IS DISTINCT FROM OLD.traffic_percentage
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

DROP TRIGGER IF EXISTS experiments_enforce_enrollment_immutability
    ON experiments;
CREATE TRIGGER experiments_enforce_enrollment_immutability
BEFORE UPDATE OF status, bucket_by, traffic_percentage, targeting_rules_json,
    minimum_exposure_config_version ON experiments
FOR EACH ROW EXECUTE FUNCTION
    public.apdl_enforce_experiment_enrollment_immutability();

COMMENT ON COLUMN experiments.bucket_by IS
    'Explicit immutable-after-draft experiment actor identity: anonymous_id or user_id';
COMMENT ON FUNCTION public.apdl_enforce_experiment_enrollment_immutability() IS
    'Rejects bucketing identity, traffic, targeting, or exposure-version changes after draft';
