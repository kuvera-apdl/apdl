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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.framework.context import AgentContext
from app.tools import clickhouse
from app.tools.experiments import get_active_experiments
from app.tools.flags import get_active_flags

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
    )
}


#: Cap on preset (deterministic) tool calls per custom agent — presets run on
#: EVERY run before reasoning, so each one is a guaranteed warehouse query.
MAX_PRESET_TOOLS = 10


def validate_preset_tools(entries: list[Any]) -> list[dict[str, Any]]:
    """Validate a preset-tools selection: ``{"tool": name, "params": {...}}``.

    Presets are the deterministic counterpart to the agentic allowed-tools
    selection: the author fixes the tool AND its parameters in the wizard, and
    the framework executes them verbatim on every run. Because nothing can
    correct a bad call at run time (unlike the loop, where the model sees the
    error and retries), params are validated against the tool's schema HERE,
    at authoring time. Raises ``ValueError`` aggregating every problem; returns
    the normalized entries.
    """
    errors: list[str] = []
    normalized: list[dict[str, Any]] = []
    if len(entries) > MAX_PRESET_TOOLS:
        errors.append(f"preset_tools: at most {MAX_PRESET_TOOLS} preset calls allowed")
    for index, entry in enumerate(entries[:MAX_PRESET_TOOLS]):
        if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
            errors.append(f"preset_tools[{index}]: expected {{'tool': name, 'params': {{...}}}}")
            continue
        name = entry["tool"]
        spec = TOOL_CATALOG.get(name)
        if spec is None:
            errors.append(f"preset_tools[{index}]: unknown tool {name!r}")
            continue
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"preset_tools[{index}]: params must be an object")
            continue
        try:
            spec.params_model.model_validate(params)
        except ValidationError as exc:
            issues = "; ".join(
                f"{'.'.join(str(loc) for loc in err['loc']) or 'params'}: {err['msg']}"
                for err in exc.errors()
            )
            errors.append(f"preset_tools[{index}] ({name}): {issues}")
            continue
        normalized.append({"tool": name, "params": params})
    if errors:
        raise ValueError("; ".join(errors))
    return normalized


def validate_tool_names(tools: list[Any]) -> list[str]:
    """Validate an allowed-tools selection (names only) against the catalog.

    Custom agents are agentic: the definition stores which catalog tools the
    reasoning model MAY call, not pre-baked invocations — params are chosen by
    the model at run time and validated per call in :func:`run_tool`. Returns
    the deduplicated names in catalog order (a stable, author-independent
    order). Raises ``ValueError`` aggregating every problem so the wizard can
    show them all at once.
    """
    errors: list[str] = []
    selected: set[str] = set()
    for index, entry in enumerate(tools):
        if not isinstance(entry, str) or entry not in TOOL_CATALOG:
            errors.append(f"tools[{index}]: unknown tool {entry!r}")
            continue
        selected.add(entry)
    if errors:
        raise ValueError("; ".join(errors))
    return [name for name in TOOL_CATALOG if name in selected]


def llm_tool_schemas(tool_names: Sequence[str]) -> list[dict[str, Any]]:
    """Neutral function-calling specs for the given catalog tools.

    The shape is what :func:`app.llm.router.chat_completion_with_tools`
    consumes: ``{"name", "description", "parameters"}`` with parameters as a
    JSON schema. Unknown names raise — callers validate selections first.
    """
    schemas: list[dict[str, Any]] = []
    for name in tool_names:
        spec = TOOL_CATALOG.get(name)
        if spec is None:
            raise ValueError(f"Unknown tool '{name}'")
        schemas.append(
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.params_model.model_json_schema(),
            }
        )
    return schemas


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
