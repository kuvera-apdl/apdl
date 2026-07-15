"""Persistence for user-defined custom agents (``custom_agents``).

A custom agent is a declarative, project-scoped, read-only analysis agent
authored in the admin console: prompts + a selection of read-only gather
tools + model tier + pipeline position. Rows here are hydrated into
:class:`app.framework.custom.CustomAgent` instances by the supervisor at
run time, so an edit takes effect on the next run without a restart.

Slug uniqueness is enforced only among ``active`` rows (partial unique
index) so archive-then-recreate under the same slug works.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

CUSTOM_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS custom_agents (
    agent_id             TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL,
    slug                 TEXT NOT NULL,
    display_name         TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    system_prompt        TEXT NOT NULL,
    user_prompt_template TEXT NOT NULL,
    model_tier           TEXT NOT NULL DEFAULT 'reasoning'
                         CHECK (model_tier IN ('fast', 'reasoning')),
    tools                JSONB NOT NULL DEFAULT '[]',
    requires             JSONB NOT NULL DEFAULT '[]',
    produces             TEXT NOT NULL,
    memory_query         TEXT,
    memory_top_k         INTEGER NOT NULL DEFAULT 5,
    pipeline_order       INTEGER NOT NULL DEFAULT 100,
    max_tool_steps       INTEGER NOT NULL DEFAULT 8,
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'archived')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# Separate statement: asyncpg's simple-query protocol handles multi-statement
# strings, but keeping DDL units small mirrors ensure_agent_memory_schema.
CUSTOM_AGENTS_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_agents_project_slug
    ON custom_agents (project_id, slug) WHERE status = 'active';
"""

# Idempotent forward migration for databases that booted an older build
# (the table DDL above is CREATE IF NOT EXISTS, so it never alters).
CUSTOM_AGENTS_MIGRATE_DDL = """
ALTER TABLE custom_agents
    ADD COLUMN IF NOT EXISTS max_tool_steps INTEGER NOT NULL DEFAULT 8;
"""


class SlugConflictError(Exception):
    """Another active custom agent in the project already uses this slug."""


_COLUMNS = (
    "agent_id, project_id, slug, display_name, description, system_prompt, "
    "user_prompt_template, model_tier, tools, requires, produces, "
    "memory_query, memory_top_k, pipeline_order, max_tool_steps, status, "
    "created_at, updated_at"
)

# Fields writable via create/update — everything else is server-managed.
_SPEC_FIELDS = (
    "slug",
    "display_name",
    "description",
    "system_prompt",
    "user_prompt_template",
    "model_tier",
    "tools",
    "requires",
    "produces",
    "memory_query",
    "memory_top_k",
    "pipeline_order",
    "max_tool_steps",
)
_JSONB_FIELDS = {"tools", "requires"}


def _normalize_tools(value: Any) -> list[str]:
    """Coerce the tools column to a list of tool names.

    Rows written before the agentic rework stored ``[{"tool": name, "params":
    {...}}, ...]``; the params are obsolete (the model now chooses them at run
    time) but the selection itself survives as the allowed-tools list.
    """
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("tool"), str):
            names.append(entry["tool"])
    return names


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """Convert a row to a dict, parsing JSONB columns defensively.

    asyncpg returns JSONB as str unless a codec is registered; a malformed
    value degrades to an empty list rather than killing the caller (same
    defense as the supervisor's _load_prior_results).
    """
    out = dict(row)
    for key in _JSONB_FIELDS:
        value = out.get(key)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                logger.error("custom_agents.%s malformed for %s", key, out.get("agent_id"))
                value = []
        out[key] = value if isinstance(value, list) else []
    out["tools"] = _normalize_tools(out["tools"])
    return out


def _spec_args(fields: dict[str, Any]) -> list[Any]:
    """Ordered parameter values for _SPEC_FIELDS, JSONB fields serialized."""
    args: list[Any] = []
    for key in _SPEC_FIELDS:
        value = fields.get(key)
        if key in _JSONB_FIELDS:
            args.append(json.dumps(value or [], default=str))
        else:
            args.append(value)
    return args


async def create_custom_agent(
    pool: asyncpg.Pool, project_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Insert a new active custom agent; raises SlugConflictError on slug reuse."""
    agent_id = str(uuid.uuid4())
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO custom_agents
                    (agent_id, project_id, slug, display_name, description,
                     system_prompt, user_prompt_template, model_tier, tools,
                     requires, produces, memory_query, memory_top_k,
                     pipeline_order, max_tool_steps)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb,
                        $11, $12, $13, $14, $15)
                RETURNING {_COLUMNS}
                """,
                agent_id,
                project_id,
                *_spec_args(fields),
            )
    except asyncpg.UniqueViolationError as exc:
        raise SlugConflictError(
            f"An active custom agent with slug '{fields.get('slug')}' already exists."
        ) from exc
    return _row_to_dict(row)


async def get_custom_agent(pool: asyncpg.Pool, agent_id: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM custom_agents WHERE agent_id = $1", agent_id
        )
    return _row_to_dict(row) if row is not None else None


async def get_active_by_slug(
    pool: asyncpg.Pool, project_id: str, slug: str
) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_COLUMNS} FROM custom_agents
            WHERE project_id = $1 AND slug = $2 AND status = 'active'
            """,
            project_id,
            slug,
        )
    return _row_to_dict(row) if row is not None else None


async def list_custom_agents(
    pool: asyncpg.Pool, project_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    status_clause = "" if include_archived else "AND status = 'active'"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM custom_agents
            WHERE project_id = $1 {status_clause}
            ORDER BY pipeline_order, slug
            """,
            project_id,
        )
    return [_row_to_dict(r) for r in rows]


async def fetch_active_by_slugs(
    pool: asyncpg.Pool, project_id: str, slugs: list[str]
) -> dict[str, dict[str, Any]]:
    """Resolve slugs to active definitions in one query (trigger + supervisor)."""
    if not slugs:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_COLUMNS} FROM custom_agents
            WHERE project_id = $1 AND slug = ANY($2::text[]) AND status = 'active'
            """,
            project_id,
            slugs,
        )
    return {r["slug"]: _row_to_dict(r) for r in rows}


async def update_custom_agent(
    pool: asyncpg.Pool, agent_id: str, fields: dict[str, Any]
) -> dict[str, Any] | None:
    """Full-replace update of the spec fields; returns the new row or None."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE custom_agents
                SET slug = $2, display_name = $3, description = $4,
                    system_prompt = $5, user_prompt_template = $6,
                    model_tier = $7, tools = $8::jsonb, requires = $9::jsonb,
                    produces = $10, memory_query = $11,
                    memory_top_k = $12, pipeline_order = $13,
                    max_tool_steps = $14, updated_at = now()
                WHERE agent_id = $1
                RETURNING {_COLUMNS}
                """,
                agent_id,
                *_spec_args(fields),
            )
    except asyncpg.UniqueViolationError as exc:
        raise SlugConflictError(
            f"An active custom agent with slug '{fields.get('slug')}' already exists."
        ) from exc
    return _row_to_dict(row) if row is not None else None


async def archive_custom_agent(pool: asyncpg.Pool, agent_id: str) -> bool:
    """Soft-delete: archived agents stop resolving in trigger/supervisor."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE custom_agents
            SET status = 'archived', updated_at = now()
            WHERE agent_id = $1 AND status = 'active'
            """,
            agent_id,
        )
    return result.endswith(" 1")
