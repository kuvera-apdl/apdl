"""Single-authority topology contracts for the ClickHouse writer."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

import clickhouse_writer as writer_module


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_declared_writer_topology_is_exactly_one_replica() -> None:
    streams = (REPOSITORY_ROOT / "pipeline/redis/streams.yaml").read_text()
    compose = (REPOSITORY_ROOT / "infra/docker/docker-compose.yml").read_text()
    writer_service = compose.split("\n  clickhouse-writer:\n", 1)[1].split(
        "\n  admin-api:\n",
        1,
    )[0]

    assert "consumer_group: clickhouse-writer" in streams
    assert "required_consumer_groups: 1" in streams
    assert "consumers_per_group: 1" in streams
    assert "replicas: 1" in writer_service
    assert "POSTGRES_URL:" in writer_service


def test_singleton_authority_rejects_a_second_writer() -> None:
    class Connection:
        def __init__(self, acquired: bool) -> None:
            self.acquired = acquired
            self.query = ""
            self.lock_id = 0

        async def fetchval(self, query: str, lock_id: int) -> bool:
            self.query = query
            self.lock_id = lock_id
            return self.acquired

    async def scenario() -> None:
        first = Connection(True)
        await writer_module._acquire_writer_singleton(first)
        assert first.query == "SELECT pg_try_advisory_lock($1)"
        assert first.lock_id == writer_module.WRITER_SINGLETON_LOCK_ID

        second = Connection(False)
        with pytest.raises(RuntimeError, match="Another ClickHouse writer"):
            await writer_module._acquire_writer_singleton(second)

    asyncio.run(scenario())


def test_singleton_lock_is_enforced_by_live_postgres() -> None:
    postgres_url = os.environ.get("APDL_WRITER_SINGLETON_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("live writer-singleton PostgreSQL URL is not configured")

    async def scenario() -> None:
        first = await asyncpg.connect(postgres_url)
        second = await asyncpg.connect(postgres_url)
        try:
            await writer_module._acquire_writer_singleton(first)
            with pytest.raises(RuntimeError, match="Another ClickHouse writer"):
                await writer_module._acquire_writer_singleton(second)

            released = await first.fetchval(
                "SELECT pg_advisory_unlock($1)",
                writer_module.WRITER_SINGLETON_LOCK_ID,
            )
            assert released is True
            await writer_module._acquire_writer_singleton(second)
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())
