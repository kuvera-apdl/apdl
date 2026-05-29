-- Migration 003: Experiment exposures table
CREATE TABLE IF NOT EXISTS experiment_exposures (
    project_id      String,
    experiment_id   String,
    user_id         String,
    variant         LowCardinality(String),
    first_exposure  DateTime64(3),
    event_date      Date DEFAULT toDate(first_exposure)
) ENGINE = ReplacingMergeTree(first_exposure)
PARTITION BY (project_id, toYYYYMM(event_date))
ORDER BY (project_id, experiment_id, user_id);
