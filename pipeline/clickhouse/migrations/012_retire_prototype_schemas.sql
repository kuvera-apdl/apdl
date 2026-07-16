-- Migration 012: remove disconnected prototype durable schemas.
--
-- These objects never had a deployed writer or query path. The supported
-- developer-preview contract is the flat events pipeline plus PostgreSQL
-- authority for flags, experiments, and agent effects, so retaining the
-- prototypes would leave two apparently durable contracts after an upgrade.
DROP VIEW IF EXISTS flag_evaluations_v;
DROP VIEW IF EXISTS experiment_exposures_v;
DROP VIEW IF EXISTS agent_actions_v;
DROP VIEW IF EXISTS personalizations_v;
DROP TABLE IF EXISTS events_dlq_v2;
DROP TABLE IF EXISTS events_v2;
DROP TABLE IF EXISTS decisions_v2;
DROP TABLE IF EXISTS feeds_v2;
