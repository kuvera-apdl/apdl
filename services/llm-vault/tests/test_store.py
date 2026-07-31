"""Unit coverage for PostgreSQL-backed vault reads."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.store import ProjectLlmVaultStore


ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000002")


@pytest.mark.asyncio
async def test_list_awaits_each_connection_response() -> None:
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    conn = SimpleNamespace(
        fetch=AsyncMock(return_value=[{"connection_id": CONNECTION_ID}]),
        fetchrow=AsyncMock(
            side_effect=[
                {
                    "owner_user_id": ACTOR_ID,
                    "active": True,
                    "roles": [],
                },
                {
                    "connection_id": CONNECTION_ID,
                    "project_id": "demo",
                    "provider": "xai",
                    "label": "main",
                    "version": 1,
                    "inventory_version": 1,
                    "state": "active",
                    "consumers": ["agents", "codegen"],
                    "validated_at": now,
                    "created_at": now,
                    "updated_at": now,
                    "revoked_at": None,
                    "model_count": 1,
                },
            ]
        ),
    )

    @asynccontextmanager
    async def acquire():
        yield conn

    store = ProjectLlmVaultStore(
        SimpleNamespace(acquire=acquire), cast(Any, object())
    )

    result = await store.list("demo", ACTOR_ID)

    assert len(result.connections) == 1
    assert result.connections[0].connection_id == CONNECTION_ID
    assert result.connections[0].provider == "xai"
    assert result.connections[0].consumers == ("agents", "codegen")
