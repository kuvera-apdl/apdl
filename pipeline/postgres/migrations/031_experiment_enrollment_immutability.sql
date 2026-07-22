-- Migration 031: database-authoritative experiment enrollment immutability.
--
-- Traffic allocation and targeting define the enrolled population. Once an
-- experiment leaves draft, PostgreSQL rejects changes to either field even if
-- a caller bypasses or races the Config router.

CREATE OR REPLACE FUNCTION public.apdl_enforce_experiment_enrollment_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $apdl_enforce_experiment_enrollment_immutability$
BEGIN
    IF OLD.status <> 'draft'
       AND (
           NEW.traffic_percentage IS DISTINCT FROM OLD.traffic_percentage
           OR NEW.targeting_rules_json IS DISTINCT FROM OLD.targeting_rules_json
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
BEFORE UPDATE OF status, traffic_percentage, targeting_rules_json ON experiments
FOR EACH ROW EXECUTE FUNCTION
    public.apdl_enforce_experiment_enrollment_immutability();

COMMENT ON FUNCTION public.apdl_enforce_experiment_enrollment_immutability() IS
    'Rejects traffic or targeting changes after an experiment leaves draft';
