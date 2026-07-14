"""Parameterized SQL templates and builders for ClickHouse analytics queries."""

from __future__ import annotations

from typing import Any

from app.clickhouse.selectors import build_selector_condition, selector_label
from app.models.schemas import EventSelector

# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------


def canonical_actor_sql(table_alias: str = "") -> str:
    """Return the one non-stitching actor identity used by Query analytics."""
    prefix = f"{table_alias}." if table_alias else ""
    return (
        f"if({prefix}user_id != '', concat('u:', {prefix}user_id), "
        f"if({prefix}anonymous_id != '', "
        f"concat('a:', {prefix}anonymous_id), ''))"
    )

def build_event_count_query(selectors: list[EventSelector], params: dict[str, Any]) -> str:
    """Build selector rows plus one exact range-wide total row."""
    subqueries: list[str] = []
    selector_conditions: list[str] = []
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
        selector_conditions.append(condition)
        subqueries.append(
            f"""
SELECT
    0 AS is_total,
    %({label_param})s AS selector,
    %({prefix}_event_name)s AS event_name,
    count() AS event_count,
    uniqExactIf({canonical_actor_sql()}, {canonical_actor_sql()} != '') AS unique_users
FROM events FINAL
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND {condition}
"""
        )

    total_condition = "\n      OR ".join(
        f"({condition})" for condition in selector_conditions
    )
    subqueries.append(
        f"""
SELECT
    1 AS is_total,
    '' AS selector,
    '' AS event_name,
    count() AS event_count,
    uniqExactIf({canonical_actor_sql()}, {canonical_actor_sql()} != '') AS unique_users
FROM events FINAL
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND (
      {total_condition}
  )
"""
    )

    return f"""
SELECT *
FROM (
{'\nUNION ALL\n'.join(subqueries)}
)
ORDER BY is_total, event_count DESC
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
    uniqExactIf({canonical_actor_sql()}, {canonical_actor_sql()} != '') AS unique_users
FROM events FINAL
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
    uniqExactIf({canonical_actor_sql()}, {canonical_actor_sql()} != '') AS unique_users
FROM events FINAL
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
        {canonical_actor_sql()} AS actor_id,
        windowFunnel({window_milliseconds})(
            toUInt64(toUnixTimestamp64Milli(timestamp)),
            {conditions}
        ) AS depth
    FROM events FINAL
    WHERE project_id = %(project_id)s
      AND event_date BETWEEN %(start_date)s AND %(end_date)s
      AND {canonical_actor_sql()} != ''
      AND (
          {prefilter}
      )
    GROUP BY actor_id
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
            {canonical_actor_sql()} AS actor_id,
            min(event_date) AS cohort_date
        FROM events FINAL
        WHERE project_id = %(project_id)s
          AND {cohort_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {canonical_actor_sql()} != ''
        GROUP BY actor_id
    ),
    activity AS (
        SELECT DISTINCT
            {canonical_actor_sql()} AS actor_id,
            event_date AS activity_date
        FROM events FINAL
        WHERE project_id = %(project_id)s
          AND {return_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {canonical_actor_sql()} != ''
    ),
    cohort_sizes AS (
        SELECT cohort_date, uniqExact(actor_id) AS cohort_size
        FROM cohort
        GROUP BY cohort_date
    )
SELECT
    c.cohort_date,
    cs.cohort_size,
    dateDiff('day', c.cohort_date, a.activity_date) AS period_offset,
    uniqExact(a.actor_id) AS active_users
FROM cohort c
INNER JOIN cohort_sizes cs ON cs.cohort_date = c.cohort_date
LEFT JOIN activity a ON c.actor_id = a.actor_id
GROUP BY c.cohort_date, cs.cohort_size, period_offset
ORDER BY c.cohort_date, period_offset
"""


def _build_retention_week_query(cohort_condition: str, return_condition: str) -> str:
    return f"""
WITH
    cohort AS (
        SELECT
            {canonical_actor_sql()} AS actor_id,
            toMonday(min(event_date)) AS cohort_week
        FROM events FINAL
        WHERE project_id = %(project_id)s
          AND {cohort_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {canonical_actor_sql()} != ''
        GROUP BY actor_id
    ),
    activity AS (
        SELECT DISTINCT
            {canonical_actor_sql()} AS actor_id,
            toMonday(event_date) AS activity_week
        FROM events FINAL
        WHERE project_id = %(project_id)s
          AND {return_condition}
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {canonical_actor_sql()} != ''
    ),
    cohort_sizes AS (
        SELECT cohort_week, uniqExact(actor_id) AS cohort_size
        FROM cohort
        GROUP BY cohort_week
    )
SELECT
    c.cohort_week,
    cs.cohort_size,
    dateDiff('week', c.cohort_week, a.activity_week) AS period_offset,
    uniqExact(a.actor_id) AS active_users
FROM cohort c
INNER JOIN cohort_sizes cs ON cs.cohort_week = c.cohort_week
LEFT JOIN activity a ON c.actor_id = a.actor_id
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
WITH
    matched AS (
        SELECT
            JSONExtractString(properties, %(cohort_property)s) AS cohort_value,
            toStartOfInterval(timestamp, INTERVAL 1 DAY) AS day,
            {canonical_actor_sql()} AS actor_id
        FROM events FINAL
        WHERE project_id = %(project_id)s
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {condition}
          AND JSONHas(properties, %(cohort_property)s)
          AND {canonical_actor_sql()} != ''
    ),
    daily AS (
        SELECT
            cohort_value,
            day,
            count() AS event_count,
            uniqExact(actor_id) AS unique_users
        FROM matched
        GROUP BY cohort_value, day
    ),
    totals AS (
        SELECT cohort_value, uniqExact(actor_id) AS total_users
        FROM matched
        GROUP BY cohort_value
    )
SELECT
    daily.cohort_value,
    daily.day,
    daily.event_count,
    daily.unique_users,
    totals.total_users
FROM daily
INNER JOIN totals USING (cohort_value)
ORDER BY daily.cohort_value, daily.day
"""


# ---------------------------------------------------------------------------
# Event discovery
# ---------------------------------------------------------------------------

def build_event_catalog_query(params: dict[str, Any]) -> str:
    """List distinct event names with volume, most frequent first."""
    return f"""
SELECT
    event_name,
    count() AS event_count,
    uniqExactIf({canonical_actor_sql()}, {canonical_actor_sql()} != '') AS unique_users
FROM events FINAL
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY event_name
ORDER BY event_count DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Experiment queries
# ---------------------------------------------------------------------------

EXPERIMENT_ANALYSIS_QUERY = f"""
WITH
    fromUnixTimestamp64Milli(%(start_ms)s, 'UTC') AS analysis_start,
    fromUnixTimestamp64Milli(%(end_ms)s, 'UTC') AS analysis_end,
    raw_exposures AS (
        SELECT
            {canonical_actor_sql()} AS actor_id,
            variant,
            first_exposure,
            message_id
        FROM feature_flag_exposures FINAL
        WHERE project_id = %(project_id)s
          AND flag_key = %(flag_key)s
          AND first_exposure >= analysis_start
          AND first_exposure < analysis_end
          AND event_date BETWEEN toDate(analysis_start) AND toDate(analysis_end)
          AND {canonical_actor_sql()} != ''
    ),
    assignments AS (
        SELECT
            actor_id,
            argMin(variant, tuple(first_exposure, message_id)) AS assigned_variant,
            min(first_exposure) AS assigned_at,
            uniqExact(variant) > 1 AS crossed_over
        FROM raw_exposures
        GROUP BY actor_id
    ),
    metric_events AS (
        SELECT
            {canonical_actor_sql()} AS actor_id,
            timestamp
        FROM events FINAL
        WHERE project_id = %(project_id)s
          AND event_name = %(metric_event)s
          AND timestamp >= analysis_start
          AND timestamp < analysis_end
          AND event_date BETWEEN toDate(analysis_start) AND toDate(analysis_end)
          AND {canonical_actor_sql()} != ''
    ),
    actor_outcomes AS (
        SELECT
            a.actor_id,
            a.assigned_variant,
            a.crossed_over,
            countIf(
                m.timestamp >= a.assigned_at
                AND m.timestamp < analysis_end
            ) > 0 AS converted
        FROM assignments AS a
        LEFT JOIN metric_events AS m ON m.actor_id = a.actor_id
        GROUP BY
            a.actor_id,
            a.assigned_variant,
            a.crossed_over,
            a.assigned_at
    )
SELECT
    assigned_variant AS variant,
    count() AS sample_size,
    countIf(converted) AS conversions,
    countIf(crossed_over) AS crossover_actors
FROM actor_outcomes
GROUP BY assigned_variant
ORDER BY assigned_variant
"""


# ---------------------------------------------------------------------------
# Feature flag guardrail queries
# ---------------------------------------------------------------------------

FEATURE_FLAG_FRONTEND_ERROR_GUARDRAIL_QUERY = """
WITH
    exposures AS (
        SELECT
            if(
                session_id != '',
                concat('s:', session_id),
                {exposure_actor}
            ) AS assignment_id,
            argMin(variant, tuple(first_exposure, message_id)) AS variant,
            min(first_exposure) AS exposure_time
        FROM feature_flag_exposures FINAL
        WHERE project_id = %(project_id)s
          AND flag_key = %(flag_key)s
          AND variant != ''
          AND first_exposure >= subtractMinutes(now(), %(window_minutes)s)
          AND event_date >= toDate(subtractMinutes(now(), %(window_minutes)s))
          AND (session_id != '' OR {exposure_actor} != '')
          {exposure_scope_filter}
        GROUP BY assignment_id
    ),
    exposure_failures AS (
        SELECT
            e.assignment_id AS assignment_id,
            e.variant AS variant,
            countIf(
                f.timestamp >= e.exposure_time
                AND f.timestamp >= subtractMinutes(now(), %(window_minutes)s)
                AND f.event_date >= toDate(subtractMinutes(now(), %(window_minutes)s))
            ) AS failure_count
        FROM exposures e
        LEFT JOIN frontend_health_events AS f FINAL
            ON f.project_id = %(project_id)s
           AND if(
                f.session_id != '',
                concat('s:', f.session_id),
                {health_actor}
           ) = e.assignment_id
           AND f.event_name = '$frontend_error'
           AND JSONHas(f.active_flags, %(flag_key)s)
           AND JSONExtractString(f.active_flags, %(flag_key)s) = e.variant
           {health_scope_filter}
        GROUP BY e.assignment_id, e.variant
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
        exposure_actor=canonical_actor_sql(),
        health_actor=canonical_actor_sql("f"),
    )
