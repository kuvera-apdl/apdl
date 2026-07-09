"""Custom agent definitions — CRUD, combined listing, and dry-run testing.

Registered *before* the run routers in ``main.py``: the run routers own
wildcard shapes like ``GET /v1/agents/{run_id}/status``, and ``/custom`` /
``/definitions`` must win deterministically over them. Within this router,
``/custom/test`` is declared before ``/custom/{agent_id}`` for the same
reason.

Project scoping comes from a verified credential. Any ``project_id`` query or
body field is only a tenant assertion and must match that credential.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.auth import require_project
from app.framework import (
    AgentContext,
    CustomAgent,
    registered_agents,
    validate_definition,
)
from app.framework.tool_catalog import catalog_descriptions, llm_tool_schemas
from app.framework.tool_loop import run_preset_tools, run_tool_loop
from app.safety.audit import AuditLogger
from app.store.custom_agents import (
    SlugConflictError,
    archive_custom_agent,
    create_custom_agent,
    get_custom_agent,
    list_custom_agents,
    update_custom_agent,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/agents", tags=["custom-agents"])


class PresetToolCall(BaseModel):
    """One deterministic preset call: a catalog tool with fixed params.

    Shape-only here — catalog membership and per-tool param validation happen
    in ``validate_definition`` so problems aggregate into one 422.
    """

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)


class CustomAgentSpec(BaseModel):
    """Create/update body. Domain rules live in ``validate_definition`` so the
    wizard gets every problem in one aggregated 422, not pydantic's first.

    ``tools`` is the ALLOWED-tools selection (catalog names the reasoning
    model may call in its tool loop); an empty list allows the whole catalog.
    ``preset_tools`` are deterministic calls executed verbatim on every run,
    before reasoning, with results rendered into the prompt.
    """

    slug: str
    display_name: str
    description: str = ""
    system_prompt: str
    user_prompt_template: str
    model_tier: str = "reasoning"
    tools: list[str] = Field(default_factory=list)
    preset_tools: list[PresetToolCall] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    produces: str
    memory_query: str | None = None
    memory_top_k: int = 5
    pipeline_order: int = 100
    max_tool_steps: int = 8


class CustomAgentOut(CustomAgentSpec):
    agent_id: str
    project_id: str
    status: str
    created_at: datetime
    updated_at: datetime


class AgentDefinition(BaseModel):
    """One entry of the combined built-in + custom listing."""

    name: str
    display_name: str
    description: str
    order: int
    produces: str
    requires: list[str]
    model_tier: str
    is_custom: bool
    agent_id: str | None = None


class DefinitionsResponse(BaseModel):
    agents: list[AgentDefinition]
    tool_catalog: list[dict[str, Any]]


class TestRunRequest(BaseModel):
    project_id: str = Field(min_length=1)
    time_range_days: int = Field(default=7, ge=1, le=90)
    definition: CustomAgentSpec


class TestRunResponse(BaseModel):
    prompt: str
    raw_response: str
    parsed_output: Any
    #: Deterministic preset calls executed before reasoning.
    preset_results: list[dict[str, Any]] = Field(default_factory=list)
    #: Calls the model chose itself inside the agentic loop.
    tool_results: list[dict[str, Any]]
    timings_ms: dict[str, int]


def _builtin_produces() -> set[str]:
    return {cls.produces for cls in registered_agents().values()}


def _spec_fields(spec: CustomAgentSpec) -> dict[str, Any]:
    return spec.model_dump()


def _validate_spec(spec: CustomAgentSpec) -> list[str]:
    return validate_definition(
        _spec_fields(spec), set(registered_agents()), _builtin_produces()
    )


async def _validate_against_project(
    pool: asyncpg.Pool,
    project_id: str,
    spec: CustomAgentSpec,
    exclude_agent_id: str | None = None,
) -> list[str]:
    """DB-dependent rules: produces uniqueness and ``requires`` resolvability.

    ``requires`` must name a built-in ``produces`` or another active custom
    agent's ``produces`` in this project — otherwise the agent would silently
    skip on every run over a typo.
    """
    errors: list[str] = []
    siblings = [
        row
        for row in await list_custom_agents(pool, project_id)
        if row["agent_id"] != exclude_agent_id
    ]
    sibling_produces = {row["produces"] for row in siblings}
    if spec.produces in sibling_produces:
        errors.append(
            f"produces '{spec.produces}' is already used by another custom agent in this project"
        )
    known_keys = _builtin_produces() | sibling_produces
    for key in spec.requires:
        if key == spec.produces:
            errors.append(f"requires '{key}' cannot reference the agent's own produces")
        elif key not in known_keys:
            errors.append(
                f"requires '{key}' does not match any built-in output or active "
                "custom agent output in this project"
            )
    return errors


@router.get("/definitions", response_model=DefinitionsResponse)
async def list_definitions(
    request: Request, project_id: str = Query(min_length=1)
) -> DefinitionsResponse:
    """Built-in + active custom agents, sorted by pipeline order.

    Feeds the console's trigger page (checkbox list) and the wizard (upstream
    ``requires`` options + tool catalog)."""
    require_project(request, project_id, "agents:read")
    pool: asyncpg.Pool = request.app.state.pg_pool
    agents = [
        AgentDefinition(
            name=name,
            display_name=name.replace("_", " ").capitalize(),
            description=cls.description,
            order=cls.order,
            produces=cls.produces,
            requires=list(cls.requires),
            model_tier=cls.model_tier,
            is_custom=False,
        )
        for name, cls in registered_agents().items()
        if cls.enabled
    ]
    for row in await list_custom_agents(pool, project_id):
        agents.append(
            AgentDefinition(
                name=row["slug"],
                display_name=row["display_name"],
                description=row["description"],
                order=row["pipeline_order"],
                produces=row["produces"],
                requires=list(row["requires"]),
                model_tier=row["model_tier"],
                is_custom=True,
                agent_id=row["agent_id"],
            )
        )
    agents.sort(key=lambda a: (a.order, a.name))
    return DefinitionsResponse(agents=agents, tool_catalog=catalog_descriptions())


@router.get("/custom", response_model=list[CustomAgentOut])
async def list_custom(
    request: Request,
    project_id: str = Query(min_length=1),
    include_archived: bool = False,
) -> list[CustomAgentOut]:
    require_project(request, project_id, "agents:read")
    pool: asyncpg.Pool = request.app.state.pg_pool
    rows = await list_custom_agents(pool, project_id, include_archived=include_archived)
    return [CustomAgentOut(**row) for row in rows]


@router.post("/custom", response_model=CustomAgentOut, status_code=201)
async def create_custom(
    body: CustomAgentSpec, request: Request, project_id: str = Query(min_length=1)
) -> CustomAgentOut:
    require_project(request, project_id, "agents:manage")
    pool: asyncpg.Pool = request.app.state.pg_pool
    errors = _validate_spec(body)
    errors += await _validate_against_project(pool, project_id, body)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    try:
        row = await create_custom_agent(pool, project_id, _spec_fields(body))
    except SlugConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "Custom agent %s (%s) created for project %s",
        row["agent_id"],
        body.slug,
        project_id,
    )
    return CustomAgentOut(**row)


@router.post("/custom/test", response_model=TestRunResponse)
async def test_custom(body: TestRunRequest, request: Request) -> TestRunResponse:
    """Dry-run a draft definition: the full agentic loop, zero persistence.

    Uniqueness checks are skipped — a draft may legitimately shadow the agent
    being edited. No ``agent_runs`` row, no audit entries (``log_tool_calls``
    off), no memory writes (CustomAgent never writes memory), so testing is
    free of side effects beyond read-only warehouse queries.
    """
    require_project(request, body.project_id, "agents:manage")
    errors = _validate_spec(body.definition)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    pool: asyncpg.Pool = request.app.state.pg_pool
    agent = CustomAgent(_spec_fields(body.definition))
    ctx = AgentContext(
        pool=pool,
        vector_store=request.app.state.vector_store,
        audit=AuditLogger(pool),  # required by the dataclass; never .log()ed here
        run_id=f"test-{uuid.uuid4()}",
        project_id=body.project_id,
        autonomy_level=1,
        time_range_days=body.time_range_days,
    )
    # Same seed the supervisor uses, so `requires`-derived placeholders render
    # as empty lists instead of leaking "{insights}" into the prompt.
    state: dict[str, Any] = {
        "project_id": body.project_id,
        "insights": [],
        "experiment_designs": [],
        "personalizations": [],
        "feature_proposals": [],
        "errors": [],
    }

    total_start = time.monotonic()
    working: dict[str, Any] = dict(state)
    working["context"] = await agent.retrieve_context(ctx)

    # Preset (deterministic) calls run before the prompt is built, exactly as
    # gather() does in a real run — but with audit logging off, like the loop.
    preset_start = time.monotonic()
    preset_trace = (
        await run_preset_tools(
            ctx,
            agent_name=agent.name,
            preset_tools=agent.preset_tools,
            log_tool_calls=False,
        )
        if agent.preset_tools
        else []
    )
    working["tool_results"] = preset_trace
    preset_ms = int((time.monotonic() - preset_start) * 1000)

    prompt = agent.build_prompt(ctx, state, working) or ""

    llm_start = time.monotonic()
    try:
        loop_result = await run_tool_loop(
            ctx,
            agent_name=agent.name,
            system_prompt=agent.system_prompt,
            user_prompt=prompt,
            tool_schemas=llm_tool_schemas(agent.agentic_tools),
            model_tier=agent.model_tier,
            max_steps=agent.max_tool_steps,
            log_tool_calls=False,
        )
    except RuntimeError as exc:
        # All providers failed — surface as a gateway error, not a 500 crash.
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc
    llm_ms = int((time.monotonic() - llm_start) * 1000)

    parsed = agent.parse(loop_result.text)
    total_ms = int((time.monotonic() - total_start) * 1000)

    def _entries(trace: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "tool": entry.tool,
                "params": entry.params,
                "result": entry.result,
                "error": entry.error,
                "elapsed_ms": entry.elapsed_ms,
            }
            for entry in trace
        ]

    return TestRunResponse(
        prompt=prompt,
        raw_response=loop_result.text,
        parsed_output=parsed,
        preset_results=_entries(preset_trace),
        tool_results=_entries(loop_result.trace),
        timings_ms={"preset_tools": preset_ms, "llm": llm_ms, "total": total_ms},
    )


@router.get("/custom/{agent_id}", response_model=CustomAgentOut)
async def get_custom(
    agent_id: str, request: Request, project_id: str = Query(min_length=1)
) -> CustomAgentOut:
    require_project(request, project_id, "agents:read")
    pool: asyncpg.Pool = request.app.state.pg_pool
    row = await get_custom_agent(pool, agent_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Custom agent not found")
    return CustomAgentOut(**row)


@router.put("/custom/{agent_id}", response_model=CustomAgentOut)
async def update_custom(
    agent_id: str,
    body: CustomAgentSpec,
    request: Request,
    project_id: str = Query(min_length=1),
) -> CustomAgentOut:
    require_project(request, project_id, "agents:manage")
    pool: asyncpg.Pool = request.app.state.pg_pool
    existing = await get_custom_agent(pool, agent_id)
    if existing is None or existing["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Custom agent not found")
    errors = _validate_spec(body)
    errors += await _validate_against_project(
        pool, project_id, body, exclude_agent_id=agent_id
    )
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    try:
        row = await update_custom_agent(pool, agent_id, _spec_fields(body))
    except SlugConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="Custom agent not found")
    return CustomAgentOut(**row)


@router.delete("/custom/{agent_id}", status_code=204)
async def archive_custom(
    agent_id: str, request: Request, project_id: str = Query(min_length=1)
) -> Response:
    """Soft-archive: the agent stops resolving in trigger/supervisor at once."""
    require_project(request, project_id, "agents:manage")
    pool: asyncpg.Pool = request.app.state.pg_pool
    existing = await get_custom_agent(pool, agent_id)
    if existing is None or existing["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Custom agent not found")
    await archive_custom_agent(pool, agent_id)
    return Response(status_code=204)
