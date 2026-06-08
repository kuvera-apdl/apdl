-- Migration 003: Retire legacy experiment exposure storage.
--
-- Canonical experiment assignment analysis now reads feature flag variant
-- exposures from feature_flag_exposures, populated by migration 006.
DROP TABLE IF EXISTS experiment_metrics_mv;
DROP TABLE IF EXISTS experiment_exposures;
