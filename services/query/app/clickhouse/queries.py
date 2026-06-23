"""Parameterized SQL templates and builders for ClickHouse analytics queries."""

from __future__ import annotations

from typing import Any

from app.clickhouse.selectors import build_selector_condition, selector_label
from app.models.schemas import EventSelector

# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------

def build_event_count_query(selectors: list[EventSelector], params: dict[str, Any]) -> str:
    """Build a count query that returns one row per event selector."""
    subqueries: list[str] = []
    for index, selector in enumerate(selectors):
        prefix = f"count_{index}"
        label_param = f"{prefix}_label"
        params[label_param] = selector_label(selector)
        # The literal event name is also aliased ``AS event_name`` below; qualify
        # the filter column so ClickHouse binds it to the events table column and
        # not that alias (otherwise the filter is always true and every selector
        # matches all events).
        condition = build_selector_condition(
            selector, params, prefix, event_name_column="events.event_name"
        )
        subqueries.append(
            f"""
SELECT
    %({label_param})s AS selector,
    %({prefix}_event_name)s AS event_name,
    count() AS event_count,
    uniq(if(user_id != '', user_id, anonymous_id)) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND {condition}
"""
        )

    return f"""
SELECT *
FROM (
{'\nUNION ALL\n'.join(subqueries)}
)
ORDER BY event_count DESC
"""


def build_event_timeseries_query(
    selector: EventSelector,
    params: dict[str, Any],
    interval: str,
) -> str:
    """Build a time-bucketed event query for one selector."""
    params["selector_label"] = selector_label(selector)
    condition = build_selector_condition(selector, params, "timeseries")
    return f"""
SELECT
    %(selector_label)s AS selector,
    toStartOfInterval(timestamp, INTERVAL {interval}) AS bucket,
    count() AS event_count,
    uniq(if(user_id != '', user_id, anonymous_id)) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND {condition}
GROUP BY bucket
ORDER BY bucket
"""


def build_event_breakdown_query(selector: EventSelector, params: dict[str, Any]) -> str:
    """Build a property breakdown query for one selector."""
    params["selector_label"] = selector_label(selector)
    condition = build_selector_condition(selector, params, "breakdown")
    return f"""
SELECT
    %(selector_label)s AS selector,
    JSONExtractString(properties, %(property)s) AS property_value,
    count() AS event_count,
    uniq(if(user_id != '', user_id, anonymous_id)) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND {condition}
GROUP BY property_value
ORDER BY event_count DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Funnel query builder
# ---------------------------------------------------------------------------

def build_funnel_query(
    steps: list[EventSelector],
    params: dict[str, Any],
    window_seconds: int = 86400 * 7,
) -> str:
    """Dynamically build an N-step funnel query using windowFunnel.

    ClickHouse's ``windowFunnel`` aggregate function efficiently computes
    the deepest step each user reached within a sliding window.

    Args:
        steps: Ordered list of event selectors defining the funnel.
        params: Parameter dictionary to populate with selector values.
        window_seconds: Maximum seconds between first and last step
                        (default 7 days).

    Returns:
        A parameterized SQL string.  The caller must supply ``project_id``,
        ``start_date``, and ``end_date`` as parameters.
    """
    step_conditions = [
        build_selector_condition(step, params, f"funnel_step_{index}")
        for index, step in enumerate(steps)
    ]
    conditions = ",\n            ".join(step_conditions)
    prefilter = "\n          OR ".join(f"({condition})" for condition in step_conditions)
    window_milliseconds = window_seconds * 1000
    return f"""
WITH funnel AS (
    SELECT
        user_id,
        windowFunnel({window_milliseconds})(
            toUInt64(toUnixTimestamp64Milli(timestamp)),
            {conditions}
        ) AS depth
    FROM events
    WHERE project_id = %(project_id)s
      AND event_date BETWEEN %(start_date)s AND %(end_date)s
      AND (
          {prefilter}
      )
    GROUP BY user_id
)
SELECT
    step_number,
    count() AS users
FROM (
    SELECT
        arrayJoin(range(1, depth + 1)) AS step_number
    FROM funnel
    WHERE depth >= 1
)
GROUP BY step_number
ORDER BY step_number
"""


# ---------------------------------------------------------------------------
# Retention query
# ---------------------------------------------------------------------------

def build_retention_query(
    cohort_selector: EventSelector,
    return_selector: EventSelector,
    params: dict[str, Any],
    *,
    period: str,
) -> str:
    """Build a day or week retention query using selectors for both event sets."""
    cohort_condition = build_selector_condition(
        cohort_selector,
        params,
        "retention_cohort",
    )
    return_condition = build_selector_condition(
        return_selector,
        params,
        "retention_return",
    )

    if period == "week":
        return _build_retention_week_query(cohort_condition, return_condition)
    return _build_retention_day_query(cohort_condition, return_condition)


def _build_retention_day_query(cohort_condition: str, return_condition: str) -> str:
    return f"""
WITH
    cohort AS (
        SELECT
            user_id,
            min(event_date) AS cohort_date
        FROM events
        WHERE project_id = %(project_id)s
          AND {cohort_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
        GROUP BY user_id
    ),
    activity AS (
        SELECT DISTINCT
            user_id,
            event_date AS activity_date
        FROM events
        WHERE project_id = %(project_id)s
          AND {return_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
    ),
    cohort_sizes AS (
        SELECT cohort_date, count(DISTINCT user_id) AS cohort_size
        FROM cohort
        GROUP BY cohort_date
    )
SELECT
    c.cohort_date,
    cs.cohort_size,
    dateDiff('day', c.cohort_date, a.activity_date) AS period_offset,
    count(DISTINCT a.user_id) AS active_users
FROM cohort c
INNER JOIN cohort_sizes cs ON cs.cohort_date = c.cohort_date
LEFT JOIN activity a ON c.user_id = a.user_id
GROUP BY c.cohort_date, cs.cohort_size, period_offset
ORDER BY c.cohort_date, period_offset
"""


def _build_retention_week_query(cohort_condition: str, return_condition: str) -> str:
    return f"""
WITH
    cohort AS (
        SELECT
            user_id,
            toMonday(min(event_date)) AS cohort_week
        FROM events
        WHERE project_id = %(project_id)s
          AND {cohort_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
        GROUP BY user_id
    ),
    activity AS (
        SELECT DISTINCT
            user_id,
            toMonday(event_date) AS activity_week
        FROM events
        WHERE project_id = %(project_id)s
          AND {return_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
    ),
    cohort_sizes AS (
        SELECT cohort_week, count(DISTINCT user_id) AS cohort_size
        FROM cohort
        GROUP BY cohort_week
    )
SELECT
    c.cohort_week,
    cs.cohort_size,
    dateDiff('week', c.cohort_week, a.activity_week) AS period_offset,
    count(DISTINCT a.user_id) AS active_users
FROM cohort c
INNER JOIN cohort_sizes cs ON cs.cohort_week = c.cohort_week
LEFT JOIN activity a ON c.user_id = a.user_id
GROUP BY c.cohort_week, cs.cohort_size, period_offset
ORDER BY c.cohort_week, period_offset
"""


# ---------------------------------------------------------------------------
# Cohort comparison query
# ---------------------------------------------------------------------------

def build_cohort_query(metric_selector: EventSelector, params: dict[str, Any]) -> str:
    """Build a cohort comparison query using a selector for the metric event."""
    condition = build_selector_condition(metric_selector, params, "cohort_metric")
    return f"""
SELECT
    JSONExtractString(properties, %(cohort_property)s) AS cohort_value,
    toStartOfInterval(timestamp, INTERVAL 1 DAY) AS day,
    count() AS event_count,
    uniq(if(user_id != '', user_id, anonymous_id)) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND {condition}
  AND JSONHas(properties, %(cohort_property)s)
GROUP BY cohort_value, day
ORDER BY cohort_value, day
"""


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

def build_event_catalog_query(params: dict[str, Any]) -> str:
    """List distinct event names with volume, most frequent first."""
    return """
SELECT
    event_name,
    count() AS event_count,
    uniq(if(user_id != '', user_id, anonymous_id)) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY event_name
ORDER BY event_count DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Experiment queries
# ---------------------------------------------------------------------------

EXPERIMENT_EXPOSURES_QUERY = """
WITH
    exposures AS (
        SELECT
            if(user_id != '', user_id, anonymous_id) AS assignment_id,
            variant,
            min(first_exposure) AS first_exposure
        FROM feature_flag_exposures
        WHERE project_id = %(project_id)s
          AND flag_key = %(flag_key)s
          AND (user_id != '' OR anonymous_id != '')
        GROUP BY assignment_id, variant
    )
SELECT
    assignment_id AS user_id,
    variant,
    first_exposure
FROM exposures
"""

EXPERIMENT_METRICS_QUERY = """
WITH
    exposures AS (
        SELECT
            if(user_id != '', user_id, anonymous_id) AS assignment_id,
            variant,
            min(first_exposure) AS first_exposure
        FROM feature_flag_exposures
        WHERE project_id = %(project_id)s
          AND flag_key = %(flag_key)s
          AND (user_id != '' OR anonymous_id != '')
        GROUP BY assignment_id, variant
    )
SELECT
    e.variant,
    e.assignment_id AS user_id,
    count() AS metric_value
FROM exposures e
INNER JOIN events ev
    ON ev.project_id = %(project_id)s
   AND (
       ev.user_id = e.assignment_id
       OR ev.anonymous_id = e.assignment_id
   )
   AND ev.timestamp >= e.first_exposure
   AND ev.event_name = %(metric)s
GROUP BY e.variant, e.assignment_id
"""


# ---------------------------------------------------------------------------
# Feature flag guardrail queries
# ---------------------------------------------------------------------------

FEATURE_FLAG_FRONTEND_ERROR_GUARDRAIL_QUERY = """
WITH
    exposures AS (
        SELECT
            session_id,
            variant,
            min(first_exposure) AS exposure_time
        FROM feature_flag_exposures
        WHERE project_id = %(project_id)s
          AND flag_key = %(flag_key)s
          AND variant != ''
          AND first_exposure >= subtractMinutes(now(), %(window_minutes)s)
          AND event_date >= toDate(subtractMinutes(now(), %(window_minutes)s))
          {exposure_scope_filter}
        GROUP BY session_id, variant
    ),
    exposure_failures AS (
        SELECT
            e.session_id AS session_id,
            e.variant AS variant,
            countIf(
                f.timestamp >= e.exposure_time
                AND f.timestamp >= subtractMinutes(now(), %(window_minutes)s)
                AND f.event_date >= toDate(subtractMinutes(now(), %(window_minutes)s))
            ) AS failure_count
        FROM exposures e
        LEFT JOIN frontend_health_events f
            ON f.project_id = %(project_id)s
           AND f.session_id = e.session_id
           AND f.event_name = '$frontend_error'
           AND JSONHas(f.active_flags, %(flag_key)s)
           AND JSONExtractString(f.active_flags, %(flag_key)s) = e.variant
           {health_scope_filter}
        GROUP BY e.session_id, e.variant
    ),
    variant_stats AS (
        SELECT
            variant,
            count() AS sessions,
            countIf(failure_count > 0) AS failure_sessions,
            sum(failure_count) AS failures
        FROM exposure_failures
        GROUP BY variant
    ),
    default_stats AS (
        SELECT
            sessions AS default_sessions,
            failure_sessions AS default_failure_sessions,
            failures AS default_failures
        FROM variant_stats
        WHERE variant = %(default_variant)s
    )
SELECT
    v.variant AS variant,
    %(default_variant)s AS default_variant,
    v.sessions AS variant_sessions,
    ifNull(d.default_sessions, 0) AS default_sessions,
    v.failure_sessions AS variant_failure_sessions,
    ifNull(d.default_failure_sessions, 0) AS default_failure_sessions,
    v.failures AS variant_failures,
    ifNull(d.default_failures, 0) AS default_failures
FROM variant_stats v
LEFT JOIN default_stats d ON 1 = 1
ORDER BY v.variant
"""


def build_feature_flag_frontend_error_guardrail_query(
    *,
    exposure_scope_filter: str = "",
    health_scope_filter: str = "",
) -> str:
    """Build the variant-vs-default frontend health guardrail query."""
    return FEATURE_FLAG_FRONTEND_ERROR_GUARDRAIL_QUERY.format(
        exposure_scope_filter=exposure_scope_filter,
        health_scope_filter=health_scope_filter,
    )
