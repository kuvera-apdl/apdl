"""Tests for the trusted project model-assignment command."""

from __future__ import annotations

import pytest

from scripts import assign_llm_models


@pytest.mark.asyncio
async def test_assignment_cli_acquires_both_maintenance_locks_in_order() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def execute(self, query: str, *args: object) -> None:
            calls.append((query, args))

    await assign_llm_models._acquire_maintenance_inhibitor(Connection())

    assert [args for _, args in calls] == [
        (assign_llm_models._MAINTENANCE_INHIBITOR_LOCK_ID,),
        (assign_llm_models._MAINTENANCE_GUARD_LOCK_ID,),
    ]


@pytest.mark.asyncio
async def test_assignment_cli_restores_locks_after_pool_reset() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        def get_reset_query(self) -> str:
            return "SELECT pg_advisory_unlock_all()"

        async def execute(self, query: str, *args: object) -> None:
            calls.append((query, args))

    await assign_llm_models._reset_maintenance_inhibitor(Connection())

    assert calls == [
        ("SELECT pg_advisory_unlock_all()", ()),
        (
            "SELECT pg_advisory_lock_shared($1)",
            (assign_llm_models._MAINTENANCE_INHIBITOR_LOCK_ID,),
        ),
        (
            "SELECT pg_advisory_lock_shared($1)",
            (assign_llm_models._MAINTENANCE_GUARD_LOCK_ID,),
        ),
    ]
