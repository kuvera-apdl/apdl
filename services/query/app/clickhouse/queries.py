"""Parameterized SQL templates for ClickHouse analytics queries."""

# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------

EVENT_COUNT_QUERY = """
SELECT
    event_name,
    count() AS event_count,
    uniq(user_id) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  {event_filter}
GROUP BY event_name
ORDER BY event_count DESC
"""

EVENT_TIMESERIES_QUERY = """
SELECT
    toStartOfInterval(timestamp, INTERVAL {interval}) AS bucket,
    count() AS event_count,
    uniq(user_id) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_name = %(event_name)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY bucket
ORDER BY bucket
"""

EVENT_BREAKDOWN_QUERY = """
SELECT
    JSONExtractString(properties, %(property)s) AS property_value,
    count() AS event_count,
    uniq(user_id) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_name = %(event_name)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY property_value
ORDER BY event_count DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Funnel query builder
# ---------------------------------------------------------------------------

def _sql_str(s: str) -> str:
    """Escape a string for safe embedding in a ClickHouse SQL string literal."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def build_funnel_query(steps: list[str], window_seconds: int = 86400 * 7) -> str:
    """Dynamically build an N-step funnel query using windowFunnel.

    ClickHouse's ``windowFunnel`` aggregate function efficiently computes
    the deepest step each user reached within a sliding window.

    Args:
        steps: Ordered list of event names defining the funnel.
        window_seconds: Maximum seconds between first and last step
                        (default 7 days).

    Returns:
        A parameterized SQL string.  The caller must supply ``project_id``,
        ``start_date``, and ``end_date`` as parameters.
    """
    safe_steps = [_sql_str(s) for s in steps]
    conditions = ", ".join(f"event_name = '{s}'" for s in safe_steps)
    window_milliseconds = window_seconds * 1000
    query = f"""
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
      AND event_name IN ({', '.join(f"'{s}'" for s in safe_steps)})
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
    return query


# ---------------------------------------------------------------------------
# Retention query
# ---------------------------------------------------------------------------

RETENTION_QUERY_DAY = """
WITH
    cohort AS (
        SELECT
            user_id,
            min(event_date) AS cohort_date
        FROM events
        WHERE project_id = %(project_id)s
          AND event_name = %(cohort_event)s
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
        GROUP BY user_id
    ),
    activity AS (
        SELECT DISTINCT
            user_id,
            event_date AS activity_date
        FROM events
        WHERE project_id = %(project_id)s
          AND event_name = %(return_event)s
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
    )
SELECT
    c.cohort_date,
    count(DISTINCT c.user_id) AS cohort_size,
    dateDiff('day', c.cohort_date, a.activity_date) AS period_offset,
    count(DISTINCT a.user_id) AS active_users
FROM cohort c
LEFT JOIN activity a ON c.user_id = a.user_id
    AND a.activity_date >= c.cohort_date
GROUP BY c.cohort_date, period_offset
ORDER BY c.cohort_date, period_offset
"""

RETENTION_QUERY_WEEK = """
WITH
    cohort AS (
        SELECT
            user_id,
            toMonday(min(event_date)) AS cohort_week
        FROM events
        WHERE project_id = %(project_id)s
          AND event_name = %(cohort_event)s
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
        GROUP BY user_id
    ),
    activity AS (
        SELECT DISTINCT
            user_id,
            toMonday(event_date) AS activity_week
        FROM events
        WHERE project_id = %(project_id)s
          AND event_name = %(return_event)s
          AND event_date BETWEEN %(start_date)s AND %(end_date)s
    )
SELECT
    c.cohort_week,
    count(DISTINCT c.user_id) AS cohort_size,
    dateDiff('week', c.cohort_week, a.activity_week) AS period_offset,
    count(DISTINCT a.user_id) AS active_users
FROM cohort c
LEFT JOIN activity a ON c.user_id = a.user_id
    AND a.activity_week >= c.cohort_week
GROUP BY c.cohort_week, period_offset
ORDER BY c.cohort_week, period_offset
"""


# ---------------------------------------------------------------------------
# Cohort comparison query
# ---------------------------------------------------------------------------

COHORT_QUERY = """
SELECT
    JSONExtractString(properties, %(cohort_property)s) AS cohort_value,
    toStartOfInterval(timestamp, INTERVAL 1 DAY) AS day,
    count() AS event_count,
    uniq(user_id) AS unique_users
FROM events
WHERE project_id = %(project_id)s
  AND event_name = %(metric_event)s
  AND event_date BETWEEN %(start_date)s AND %(end_date)s
  AND JSONHas(properties, %(cohort_property)s)
GROUP BY cohort_value, day
ORDER BY cohort_value, day
"""


# ---------------------------------------------------------------------------
# Experiment queries
# ---------------------------------------------------------------------------

EXPERIMENT_EXPOSURES_QUERY = """
SELECT
    user_id,
    JSONExtractString(properties, 'variant') AS variant,
    min(timestamp) AS first_exposure
FROM events
WHERE project_id = %(project_id)s
  AND event_name = '$experiment_exposure'
  AND JSONExtractString(properties, 'experiment_id') = %(experiment_id)s
GROUP BY user_id, variant
"""

EXPERIMENT_METRICS_QUERY = """
WITH
    exposures AS (
        SELECT
            user_id,
            JSONExtractString(properties, 'variant') AS variant,
            min(timestamp) AS first_exposure
        FROM events
        WHERE project_id = %(project_id)s
          AND event_name = '$experiment_exposure'
          AND JSONExtractString(properties, 'experiment_id') = %(experiment_id)s
        GROUP BY user_id, variant
    )
SELECT
    e.variant,
    e.user_id,
    count() AS metric_value
FROM exposures e
INNER JOIN events ev
    ON e.user_id = ev.user_id
    AND ev.timestamp >= e.first_exposure
    AND ev.event_name = %(metric)s
    AND ev.project_id = %(project_id)s
GROUP BY e.variant, e.user_id
"""
