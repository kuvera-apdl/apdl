"""Atomically assign one Codegen editor model and one helper model."""

from __future__ import annotations

import argparse
import asyncio

import asyncpg

from app.config import postgres_url
from app.store.llm_routing import assign_project_models


_MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
_MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


async def _acquire_maintenance_inhibitor(connection: asyncpg.Connection) -> None:
    await connection.execute(
        "SELECT pg_advisory_lock_shared($1)",
        _MAINTENANCE_INHIBITOR_LOCK_ID,
    )
    await connection.execute(
        "SELECT pg_advisory_lock_shared($1)",
        _MAINTENANCE_GUARD_LOCK_ID,
    )


async def _reset_maintenance_inhibitor(connection: asyncpg.Connection) -> None:
    """Apply asyncpg's default reset, then restore the session inhibitor."""
    reset_query = connection.get_reset_query()
    if reset_query:
        await connection.execute(reset_query)
    await _acquire_maintenance_inhibitor(connection)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--editor-provider", required=True)
    parser.add_argument("--editor-model-id", required=True)
    parser.add_argument("--helper-provider", required=True)
    parser.add_argument("--helper-model-id", required=True)
    parser.add_argument("--actor", required=True)
    return parser


async def _run(args: argparse.Namespace) -> None:
    pool = await asyncpg.create_pool(
        dsn=postgres_url(),
        min_size=1,
        max_size=1,
        init=_acquire_maintenance_inhibitor,
        reset=_reset_maintenance_inhibitor,
        max_inactive_connection_lifetime=0,
    )
    try:
        assignments = await assign_project_models(
            pool,
            project_id=args.project_id,
            editor_provider=args.editor_provider,
            editor_model_id=args.editor_model_id,
            helper_provider=args.helper_provider,
            helper_model_id=args.helper_model_id,
            actor=args.actor,
        )
    finally:
        await pool.close()
    for assignment in assignments:
        print(
            f"{assignment.role}={assignment.provider}/{assignment.model_id} "
            f"assignment_version={assignment.assignment_version}"
        )


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
