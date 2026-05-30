"""SQL generation helpers for property-filtered event selectors."""

from __future__ import annotations

from typing import Any

from app.models.schemas import EventFilterOperator, EventPropertyFilter, EventSelector


def selector_label(selector: EventSelector) -> str:
    """Return a deterministic human-readable label for a selector."""
    if not selector.filters:
        return selector.event_name

    filter_labels = ", ".join(_filter_label(f) for f in selector.filters)
    return f"{selector.event_name}[{filter_labels}]"


def build_selector_condition(
    selector: EventSelector,
    params: dict[str, Any],
    prefix: str,
    *,
    event_name_column: str = "event_name",
    properties_column: str = "properties",
) -> str:
    """Build a parameterized ClickHouse boolean expression for a selector.

    The returned SQL uses pyformat placeholders because ClickHouseClient
    normalizes them for the async driver before execution.
    """
    event_param = f"{prefix}_event_name"
    params[event_param] = selector.event_name

    clauses = [f"{event_name_column} = %({event_param})s"]
    for index, filter_ in enumerate(selector.filters):
        clauses.append(
            _build_filter_condition(
                filter_,
                params,
                f"{prefix}_filter_{index}",
                properties_column,
            )
        )

    return "(" + " AND ".join(clauses) + ")"


def _filter_label(filter_: EventPropertyFilter) -> str:
    if filter_.operator in {EventFilterOperator.exists, EventFilterOperator.not_exists}:
        return f"{filter_.property} {filter_.operator.value}"

    value = filter_.value
    if isinstance(value, list):
        label_value = "[" + ",".join(str(item) for item in value) + "]"
    else:
        label_value = str(value)
    return f"{filter_.property} {filter_.operator.value} {label_value}"


def _build_filter_condition(
    filter_: EventPropertyFilter,
    params: dict[str, Any],
    prefix: str,
    properties_column: str,
) -> str:
    property_param = f"{prefix}_property"
    params[property_param] = filter_.property
    has_property = f"JSONHas({properties_column}, %({property_param})s)"

    operator = filter_.operator
    if operator == EventFilterOperator.exists:
        return has_property
    if operator == EventFilterOperator.not_exists:
        return f"NOT {has_property}"

    if operator == EventFilterOperator.contains:
        value_param = f"{prefix}_value"
        params[value_param] = filter_.value
        extractor = f"JSONExtractString({properties_column}, %({property_param})s)"
        return f"({has_property} AND positionCaseSensitive({extractor}, %({value_param})s) > 0)"

    if operator in {
        EventFilterOperator.gt,
        EventFilterOperator.gte,
        EventFilterOperator.lt,
        EventFilterOperator.lte,
    }:
        value_param = f"{prefix}_value"
        params[value_param] = filter_.value
        comparator = _numeric_comparator(operator)
        extractor = f"JSONExtractFloat({properties_column}, %({property_param})s)"
        return f"({has_property} AND {extractor} {comparator} %({value_param})s)"

    if operator in {EventFilterOperator.in_, EventFilterOperator.not_in}:
        value_params = []
        for index, value in enumerate(filter_.value):
            value_param = f"{prefix}_value_{index}"
            params[value_param] = _normalize_filter_value(value)
            value_params.append(f"%({value_param})s")

        extractor = _extractor_for_value(filter_.value[0], properties_column, property_param)
        comparator = "IN" if operator == EventFilterOperator.in_ else "NOT IN"
        values_sql = ", ".join(value_params)
        return f"({has_property} AND {extractor} {comparator} ({values_sql}))"

    value_param = f"{prefix}_value"
    params[value_param] = _normalize_filter_value(filter_.value)
    extractor = _extractor_for_value(filter_.value, properties_column, property_param)
    comparator = "=" if operator == EventFilterOperator.eq else "!="
    return f"({has_property} AND {extractor} {comparator} %({value_param})s)"


def _extractor_for_value(value: Any, properties_column: str, property_param: str) -> str:
    if isinstance(value, bool):
        return f"JSONExtractBool({properties_column}, %({property_param})s)"
    if isinstance(value, int | float):
        return f"JSONExtractFloat({properties_column}, %({property_param})s)"
    return f"JSONExtractString({properties_column}, %({property_param})s)"


def _normalize_filter_value(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def _numeric_comparator(operator: EventFilterOperator) -> str:
    return {
        EventFilterOperator.gt: ">",
        EventFilterOperator.gte: ">=",
        EventFilterOperator.lt: "<",
        EventFilterOperator.lte: "<=",
    }[operator]
