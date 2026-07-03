"""Tool catalog — the allowlist of gather tools a custom agent may select.

This module is the read-only security boundary for user-defined agents:
only the read-only query/list tools below are reachable from a custom
agent's ``tools`` selection. Nothing that creates or mutates state
(``create_flag``, ``create_experiment_config``, ``create_ui_config``,
anything in ``tools/code.py``) is in the catalog, so a custom agent cannot
acquire side effects no matter what its definition says.

``project_id``/``start_date``/``end_date`` are always injected from the
:class:`AgentContext` (exactly as the built-in behaviour agent computes
them) — never definition-supplied — so a custom agent cannot read another
project's data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.framework.context import AgentContext
from app.tools import clickhouse
from app.tools.experiments import get_active_experiments
from app.tools.flags import get_active_flags
from app.tools.ui_config import list_ui_configs

FilterScalar = str | int | float | bool


class EventPropertyFilter(BaseModel):
    """Mirror of ``EventPropertyFilterPayload`` (tools/clickhouse.py)."""

    property: str = Field(min_length=1, max_length=200)
    operator: Literal[
        "eq", "neq", "in", "not_in", "exists", "not_exists",
        "contains", "gt", "gte", "lt", "lte",
    ]
    value: FilterScalar | list[FilterScalar] | None = None


class EventSelector(BaseModel):
    """Mirror of ``EventSelectorPayload`` (tools/clickhouse.py)."""

    event_name: str = Field(min_length=1, max_length=200)
    filters: list[EventPropertyFilter] = Field(default_factory=list, max_length=10)

    def payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class EmptyParams(BaseModel):
    model_config = {"extra": "forbid"}


class DiscoverEventsParams(BaseModel):
    model_config = {"extra": "forbid"}
    limit: int = Field(default=100, ge=1, le=500)


class QueryEventsParams(BaseModel):
    model_config = {"extra": "forbid"}
    selectors: list[EventSelector] = Field(min_length=1, max_length=10)


class TimeseriesParams(BaseModel):
    model_config = {"extra": "forbid"}
    selector: EventSelector
    interval: Literal["1 HOUR", "1 DAY", "1 WEEK", "1 MONTH"] = "1 DAY"


class FunnelParams(BaseModel):
    model_config = {"extra": "forbid"}
    steps: list[EventSelector] = Field(min_length=2, max_length=8)
    window_days: int = Field(default=7, ge=1, le=90)


class RetentionParams(BaseModel):
    model_config = {"extra": "forbid"}
    cohort_selector: EventSelector
    return_selector: EventSelector
    period: Literal["day", "week"] = "day"


class CohortParams(BaseModel):
    model_config = {"extra": "forbid"}
    cohort_property: str = Field(min_length=1, max_length=200)
    metric_selector: EventSelector


class BreakdownParams(BaseModel):
    model_config = {"extra": "forbid"}
    selector: EventSelector
    property_name: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class UiConfigsParams(BaseModel):
    model_config = {"extra": "forbid"}
    component: str | None = Field(default=None, max_length=200)


ToolRunner = Callable[[AgentContext, BaseModel, str, str], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    runner: ToolRunner


async def _run_discover_events(
    ctx: AgentContext, p: DiscoverEventsParams, start: str, end: str
) -> Any:
    return await clickhouse.discover_events(
        project_id=ctx.project_id, start_date=start, end_date=end, limit=p.limit
    )


async def _run_query_events(ctx: AgentContext, p: QueryEventsParams, start: str, end: str) -> Any:
    return await clickhouse.query_events(
        project_id=ctx.project_id,
        start_date=start,
        end_date=end,
        selectors=[s.payload() for s in p.selectors],
    )


async def _run_timeseries(ctx: AgentContext, p: TimeseriesParams, start: str, end: str) -> Any:
    return await clickhouse.query_timeseries(
        project_id=ctx.project_id,
        selector=p.selector.payload(),
        start_date=start,
        end_date=end,
        interval=p.interval,
    )


async def _run_funnel(ctx: AgentContext, p: FunnelParams, start: str, end: str) -> Any:
    return await clickhouse.query_funnel(
        project_id=ctx.project_id,
        steps=[s.payload() for s in p.steps],
        start_date=start,
        end_date=end,
        window_days=p.window_days,
    )


async def _run_retention(ctx: AgentContext, p: RetentionParams, start: str, end: str) -> Any:
    return await clickhouse.query_retention(
        project_id=ctx.project_id,
        cohort_selector=p.cohort_selector.payload(),
        return_selector=p.return_selector.payload(),
        start_date=start,
        end_date=end,
        period=p.period,
    )


async def _run_cohort(ctx: AgentContext, p: CohortParams, start: str, end: str) -> Any:
    return await clickhouse.query_cohort(
        project_id=ctx.project_id,
        cohort_property=p.cohort_property,
        metric_selector=p.metric_selector.payload(),
        start_date=start,
        end_date=end,
    )


async def _run_breakdown(ctx: AgentContext, p: BreakdownParams, start: str, end: str) -> Any:
    return await clickhouse.query_breakdown(
        project_id=ctx.project_id,
        selector=p.selector.payload(),
        property_name=p.property_name,
        start_date=start,
        end_date=end,
        limit=p.limit,
    )


async def _run_list_flags(ctx: AgentContext, p: EmptyParams, start: str, end: str) -> Any:
    return await get_active_flags(ctx.project_id)


async def _run_active_experiments(ctx: AgentContext, p: EmptyParams, start: str, end: str) -> Any:
    return await get_active_experiments(ctx.project_id)


async def _run_list_ui_configs(ctx: AgentContext, p: UiConfigsParams, start: str, end: str) -> Any:
    return await list_ui_configs(ctx.project_id, component=p.component)


TOOL_CATALOG: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "discover_events",
            "List the event names present for the project, most frequent first.",
            DiscoverEventsParams,
            _run_discover_events,
        ),
        ToolSpec(
            "query_events",
            "Aggregated counts and unique users for selected events.",
            QueryEventsParams,
            _run_query_events,
        ),
        ToolSpec(
            "query_timeseries",
            "Time-bucketed counts for a single event.",
            TimeseriesParams,
            _run_timeseries,
        ),
        ToolSpec(
            "query_funnel",
            "Multi-step funnel conversion analysis.",
            FunnelParams,
            _run_funnel,
        ),
        ToolSpec(
            "query_retention",
            "N-day / N-week retention grid for a cohort.",
            RetentionParams,
            _run_retention,
        ),
        ToolSpec(
            "query_cohort",
            "Compare a metric across user cohorts defined by a property.",
            CohortParams,
            _run_cohort,
        ),
        ToolSpec(
            "query_breakdown",
            "Break an event down by a JSON property value.",
            BreakdownParams,
            _run_breakdown,
        ),
        ToolSpec(
            "list_flags",
            "Active feature flags configured for the project.",
            EmptyParams,
            _run_list_flags,
        ),
        ToolSpec(
            "get_active_experiments",
            "Active experiments configured for the project.",
            EmptyParams,
            _run_active_experiments,
        ),
        ToolSpec(
            "list_ui_configs",
            "Server-driven UI configurations for the project.",
            UiConfigsParams,
            _run_list_ui_configs,
        ),
    )
}


def validate_tool_selection(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate a definition's tool selection against the catalog.

    Returns the normalized selection (validated params re-dumped). Raises
    ``ValueError`` whose message aggregates every per-tool problem so the
    wizard can show them all at once.
    """
    errors: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(tools):
        name = entry.get("tool") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name not in TOOL_CATALOG:
            errors.append(f"tools[{index}]: unknown tool {name!r}")
            continue
        params = entry.get("params") or {}
        try:
            model = TOOL_CATALOG[name].params_model.model_validate(params)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            errors.append(f"tools[{index}] ({name}): {problems}")
            continue
        normalized.append({"tool": name, "params": model.model_dump(exclude_none=True)})
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def _date_window(ctx: AgentContext) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=ctx.time_range_days)
    return start.isoformat(), end.isoformat()


async def run_tool(ctx: AgentContext, name: str, params: dict[str, Any]) -> Any:
    """Execute one catalog tool with ctx-injected scoping."""
    spec = TOOL_CATALOG.get(name)
    if spec is None:
        raise ValueError(f"Unknown tool '{name}'")
    model = spec.params_model.model_validate(params or {})
    start, end = _date_window(ctx)
    return await spec.runner(ctx, model, start, end)


def catalog_descriptions() -> list[dict[str, Any]]:
    """Wizard-facing catalog: name, description, JSON schema of the params."""
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "params_schema": spec.params_model.model_json_schema(),
        }
        for spec in TOOL_CATALOG.values()
    ]
