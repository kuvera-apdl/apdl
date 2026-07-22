"""Parameterized SQL templates and builders for ClickHouse analytics queries."""

from __future__ import annotations

from typing import Any

from app.clickhouse.selectors import build_selector_condition, selector_label
from app.models.schemas import EventSelector

# ---------------------------------------------------------------------------
# Event queries
# ---------------------------------------------------------------------------


def canonical_actor_sql(table_alias: str, identity_alias: str) -> str:
    """Resolve one actor through the tenant-scoped identity-alias contract."""
    prefix = f"{table_alias}."
    resolved_prefix = f"{identity_alias}."
    return (
        f"if({prefix}user_id != '', concat('u:', {prefix}user_id), "
        f"if({resolved_prefix}resolved_user_id != '', "
        f"concat('u:', {resolved_prefix}resolved_user_id), "
        f"if({prefix}anonymous_id != '', "
        f"concat('a:', {prefix}anonymous_id), '')))"
    )


def identity_alias_join_sql(table_alias: str, identity_alias: str) -> str:
    """Join only unambiguous aliases from the authenticated request's tenant."""
    return f"""
LEFT ANY JOIN (
    SELECT project_id, anonymous_id, resolved_user_id, has_conflict
    FROM resolved_identity_aliases
    WHERE project_id = %(project_id)s
) AS {identity_alias}
    ON {identity_alias}.project_id = {table_alias}.project_id
   AND {identity_alias}.anonymous_id = {table_alias}.anonymous_id
"""


def build_event_count_query(selectors: list[EventSelector], params: dict[str, Any]) -> str:
    """Build selector rows plus one exact range-wide total row."""
    actor = canonical_actor_sql("e", "actor_identity")
    identity_join = identity_alias_join_sql("e", "actor_identity")
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
            selector, params, prefix, event_name_column="e.event_name"
        )
        selector_conditions.append(condition)
        subqueries.append(
            f"""
SELECT
    0 AS is_total,
    %({label_param})s AS selector,
    %({prefix}_event_name)s AS event_name,
    count() AS event_count,
    uniqExactIf({actor}, {actor} != '') AS unique_users
FROM events AS e FINAL
{identity_join}
WHERE e.project_id = %(project_id)s
  AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
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
    uniqExactIf({actor}, {actor} != '') AS unique_users
FROM events AS e FINAL
{identity_join}
WHERE e.project_id = %(project_id)s
  AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
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
    condition = build_selector_condition(
        selector,
        params,
        "timeseries",
        event_name_column="e.event_name",
    )
    actor = canonical_actor_sql("e", "actor_identity")
    identity_join = identity_alias_join_sql("e", "actor_identity")
    return f"""
SELECT
    %(selector_label)s AS selector,
    toStartOfInterval(e.timestamp, INTERVAL {interval}) AS bucket,
    count() AS event_count,
    uniqExactIf({actor}, {actor} != '') AS unique_users
FROM events AS e FINAL
{identity_join}
WHERE e.project_id = %(project_id)s
  AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
  AND {condition}
GROUP BY bucket
ORDER BY bucket
"""


def build_event_breakdown_query(selector: EventSelector, params: dict[str, Any]) -> str:
    """Build a typed scalar property breakdown query for one selector.

    ClickHouse's JSON functions return default values when the requested value
    has the wrong type.  Classify each value first so strings, integers,
    floating-point numbers, and booleans cannot collapse into the same bucket.
    Missing values, nulls, arrays, and objects are deliberately excluded.
    """
    params["selector_label"] = selector_label(selector)
    condition = build_selector_condition(
        selector,
        params,
        "breakdown",
        event_name_column="e.event_name",
    )
    actor = canonical_actor_sql("e", "actor_identity")
    identity_join = identity_alias_join_sql("e", "actor_identity")
    return f"""
SELECT
    selector,
    property_type,
    property_value,
    count() AS event_count,
    uniqExactIf(actor_id, actor_id != '') AS unique_users
FROM (
    SELECT
        selector,
        actor_id,
        multiIf(
            property_json_type = 'String', 'string',
            property_json_type IN ('Int64', 'UInt64'), 'integer',
            property_json_type = 'Double', 'float',
            property_json_type = 'Bool', 'boolean',
            ''
        ) AS property_type,
        multiIf(
            property_json_type = 'String',
                JSONExtractString(properties, %(property)s),
            property_json_type = 'Int64',
                toString(JSONExtractInt(properties, %(property)s)),
            property_json_type = 'UInt64',
                toString(JSONExtractUInt(properties, %(property)s)),
            property_json_type = 'Double',
                toString(JSONExtractFloat(properties, %(property)s)),
            property_json_type = 'Bool',
                if(JSONExtractBool(properties, %(property)s), 'true', 'false'),
            ''
        ) AS property_value
    FROM (
        SELECT
            %(selector_label)s AS selector,
            e.properties AS properties,
            JSONType(e.properties, %(property)s) AS property_json_type,
            {actor} AS actor_id
        FROM events AS e FINAL
        {identity_join}
        WHERE e.project_id = %(project_id)s
          AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {condition}
    )
    WHERE property_json_type
        IN ('String', 'Int64', 'UInt64', 'Double', 'Bool')
)
GROUP BY selector, property_type, property_value
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
        build_selector_condition(
            step,
            params,
            f"funnel_step_{index}",
            event_name_column="e.event_name",
        )
        for index, step in enumerate(steps)
    ]
    conditions = ",\n            ".join(step_conditions)
    prefilter = "\n          OR ".join(f"({condition})" for condition in step_conditions)
    window_milliseconds = window_seconds * 1000
    actor = canonical_actor_sql("e", "actor_identity")
    identity_join = identity_alias_join_sql("e", "actor_identity")
    return f"""
WITH funnel AS (
    SELECT
        {actor} AS actor_id,
        windowFunnel({window_milliseconds})(
            toUInt64(toUnixTimestamp64Milli(e.timestamp)),
            {conditions}
        ) AS depth
    FROM events AS e FINAL
    {identity_join}
    WHERE e.project_id = %(project_id)s
      AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
      AND {actor} != ''
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
    """Build first-match-in-window retention using selectors for both event sets."""
    cohort_condition = build_selector_condition(
        cohort_selector,
        params,
        "retention_cohort",
        event_name_column="e.event_name",
    )
    return_condition = build_selector_condition(
        return_selector,
        params,
        "retention_return",
        event_name_column="e.event_name",
    )

    if period == "week":
        return _build_retention_week_query(cohort_condition, return_condition)
    return _build_retention_day_query(cohort_condition, return_condition)


def _build_retention_day_query(cohort_condition: str, return_condition: str) -> str:
    cohort_actor = canonical_actor_sql("e", "cohort_identity")
    cohort_identity_join = identity_alias_join_sql("e", "cohort_identity")
    activity_actor = canonical_actor_sql("e", "activity_identity")
    activity_identity_join = identity_alias_join_sql("e", "activity_identity")
    return f"""
WITH
    cohort AS (
        SELECT
            {cohort_actor} AS actor_id,
            min(e.event_date) AS cohort_date
        FROM events AS e FINAL
        {cohort_identity_join}
        WHERE e.project_id = %(project_id)s
          AND {cohort_condition}
          AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {cohort_actor} != ''
        GROUP BY actor_id
    ),
    activity AS (
        SELECT DISTINCT
            {activity_actor} AS actor_id,
            e.event_date AS activity_date
        FROM events AS e FINAL
        {activity_identity_join}
        WHERE e.project_id = %(project_id)s
          AND {return_condition}
          AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {activity_actor} != ''
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
    cohort_actor = canonical_actor_sql("e", "cohort_identity")
    cohort_identity_join = identity_alias_join_sql("e", "cohort_identity")
    activity_actor = canonical_actor_sql("e", "activity_identity")
    activity_identity_join = identity_alias_join_sql("e", "activity_identity")
    return f"""
WITH
    cohort AS (
        SELECT
            {cohort_actor} AS actor_id,
            toMonday(min(e.event_date)) AS cohort_week
        FROM events AS e FINAL
        {cohort_identity_join}
        WHERE e.project_id = %(project_id)s
          AND {cohort_condition}
          AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {cohort_actor} != ''
        GROUP BY actor_id
    ),
    activity AS (
        SELECT DISTINCT
            {activity_actor} AS actor_id,
            toMonday(e.event_date) AS activity_week
        FROM events AS e FINAL
        {activity_identity_join}
        WHERE e.project_id = %(project_id)s
          AND {return_condition}
          AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {activity_actor} != ''
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
    condition = build_selector_condition(
        metric_selector,
        params,
        "cohort_metric",
        event_name_column="e.event_name",
    )
    actor = canonical_actor_sql("e", "actor_identity")
    identity_join = identity_alias_join_sql("e", "actor_identity")
    return f"""
WITH
    matched AS (
        SELECT
            JSONExtractString(e.properties, %(cohort_property)s) AS cohort_value,
            toStartOfInterval(e.timestamp, INTERVAL 1 DAY) AS day,
            {actor} AS actor_id
        FROM events AS e FINAL
        {identity_join}
        WHERE e.project_id = %(project_id)s
          AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
          AND {condition}
          AND JSONHas(e.properties, %(cohort_property)s)
          AND {actor} != ''
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
    actor = canonical_actor_sql("e", "actor_identity")
    identity_join = identity_alias_join_sql("e", "actor_identity")
    return f"""
SELECT
    e.event_name AS event_name,
    count() AS event_count,
    uniqExactIf({actor}, {actor} != '') AS unique_users
FROM events AS e FINAL
{identity_join}
WHERE e.project_id = %(project_id)s
  AND e.event_date BETWEEN %(start_date)s AND %(end_date)s
GROUP BY e.event_name
ORDER BY event_count DESC
LIMIT %(limit)s
"""


# ---------------------------------------------------------------------------
# Experiment queries
# ---------------------------------------------------------------------------

_EXPERIMENT_EXPOSURE_ACTOR = canonical_actor_sql("exposure", "exposure_identity")
_EXPERIMENT_EXPOSURE_IDENTITY_JOIN = identity_alias_join_sql(
    "exposure", "exposure_identity"
)
_EXPERIMENT_METRIC_ACTOR = canonical_actor_sql("metric", "metric_identity")
_EXPERIMENT_METRIC_IDENTITY_JOIN = identity_alias_join_sql(
    "metric", "metric_identity"
)

EXPERIMENT_ANALYSIS_QUERY = f"""
WITH
    fromUnixTimestamp64Milli(%(start_ms)s, 'UTC') AS analysis_start,
    fromUnixTimestamp64Milli(%(end_ms)s, 'UTC') AS analysis_end,
    raw_exposures AS (
        SELECT
            {_EXPERIMENT_EXPOSURE_ACTOR} AS actor_id,
            exposure.variant,
            exposure.first_exposure,
            exposure.message_id,
            ifNull(exposure_identity.has_conflict, 0) AS identity_conflict
        FROM feature_flag_exposures AS exposure FINAL
        {_EXPERIMENT_EXPOSURE_IDENTITY_JOIN}
        WHERE exposure.project_id = %(project_id)s
          AND exposure.flag_key = %(flag_key)s
          AND exposure.first_exposure >= analysis_start
          AND exposure.first_exposure < analysis_end
          AND exposure.event_date BETWEEN toDate(analysis_start) AND toDate(analysis_end)
          AND {_EXPERIMENT_EXPOSURE_ACTOR} != ''
    ),
    assignments AS (
        SELECT
            actor_id,
            argMin(variant, tuple(first_exposure, message_id)) AS assigned_variant,
            min(first_exposure) AS assigned_at,
            uniqExact(variant) > 1 AS crossed_over,
            countIf(variant NOT IN %(declared_variants)s) > 0 AS has_unknown_variant
        FROM raw_exposures
        GROUP BY actor_id
    ),
    metric_events AS (
        SELECT
            {_EXPERIMENT_METRIC_ACTOR} AS actor_id,
            metric.timestamp,
            ifNull(metric_identity.has_conflict, 0) AS identity_conflict
        FROM events AS metric FINAL
        {_EXPERIMENT_METRIC_IDENTITY_JOIN}
        WHERE metric.project_id = %(project_id)s
          AND metric.event_name = %(metric_event)s
          AND metric.timestamp >= analysis_start
          AND metric.timestamp < analysis_end
          AND metric.event_date BETWEEN toDate(analysis_start) AND toDate(analysis_end)
          AND {_EXPERIMENT_METRIC_ACTOR} != ''
    ),
    identity_quality AS (
        SELECT uniqExact(actor_id) AS identity_conflict_actors
        FROM (
            SELECT actor_id
            FROM raw_exposures
            WHERE identity_conflict = 1
            UNION ALL
            SELECT actor_id
            FROM metric_events
            WHERE identity_conflict = 1
        ) AS conflicted_actors
    ),
    actor_outcomes AS (
        SELECT
            a.actor_id,
            a.assigned_variant,
            a.crossed_over,
            a.has_unknown_variant,
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
            a.has_unknown_variant,
            a.assigned_at
    )
SELECT
    assigned_variant AS variant,
    count() AS sample_size,
    countIf(converted) AS conversions,
    countIf(crossed_over) AS crossover_actors,
    countIf(has_unknown_variant) AS unknown_variant_actors,
    identity_quality.identity_conflict_actors AS identity_conflict_actors
FROM actor_outcomes
CROSS JOIN identity_quality
GROUP BY assigned_variant, identity_quality.identity_conflict_actors
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
                exposure.session_id != '',
                concat('s:', exposure.session_id),
                {exposure_actor}
            ) AS assignment_id,
            argMin(
                exposure.variant,
                tuple(exposure.first_exposure, exposure.message_id)
            ) AS variant,
            min(exposure.first_exposure) AS exposure_time
        FROM feature_flag_exposures AS exposure FINAL
        {exposure_identity_join}
        WHERE exposure.project_id = %(project_id)s
          AND exposure.flag_key = %(flag_key)s
          AND exposure.variant != ''
          AND exposure.first_exposure >= subtractMinutes(now(), %(window_minutes)s)
          AND exposure.event_date >= toDate(subtractMinutes(now(), %(window_minutes)s))
          AND (exposure.session_id != '' OR {exposure_actor} != '')
          {exposure_scope_filter}
        GROUP BY assignment_id
    ),
    health_events AS (
        SELECT
            if(
                f.session_id != '',
                concat('s:', f.session_id),
                {health_actor}
            ) AS assignment_id,
            f.timestamp,
            f.event_date,
            f.event_name,
            f.active_flags
        FROM frontend_health_events AS f FINAL
        {health_identity_join}
        WHERE f.project_id = %(project_id)s
          {health_scope_filter}
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
        LEFT JOIN health_events AS f
            ON f.assignment_id = e.assignment_id
           AND f.event_name = '$frontend_error'
           AND JSONHas(f.active_flags, %(flag_key)s)
           AND JSONExtractString(f.active_flags, %(flag_key)s) = e.variant
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
        exposure_actor=canonical_actor_sql("exposure", "exposure_identity"),
        health_actor=canonical_actor_sql("f", "health_identity"),
        exposure_identity_join=identity_alias_join_sql(
            "exposure", "exposure_identity"
        ),
        health_identity_join=identity_alias_join_sql("f", "health_identity"),
    )
