"""Unit tests for the agent_memory schema reconciliation (fake conn, no DB)."""

import pytest

from app.main import ensure_agent_memory_schema
from app.memory.embeddings import EMBEDDING_DIMENSIONS


class FakeConn:
    """Records executed SQL and returns a scripted embedding dimension."""

    def __init__(self, current_dim):
        self._current_dim = current_dim
        self.executed: list[str] = []

    async def execute(self, sql: str, *args):
        self.executed.append(" ".join(sql.split()))

    async def fetchval(self, sql: str, *args):
        return self._current_dim


def _ran(conn, needle: str) -> bool:
    return any(needle in sql for sql in conn.executed)


@pytest.mark.asyncio
async def test_migrates_stale_dimension_and_purges_rows():
    # An old DB still on vector(1536) must be migrated to the current dimension:
    # drop index, purge incompatible rows, ALTER the column.
    conn = FakeConn(current_dim=1536)
    await ensure_agent_memory_schema(conn)

    assert _ran(conn, "DROP INDEX IF EXISTS idx_agent_memory_embedding")
    assert _ran(conn, "DELETE FROM agent_memory")
    assert _ran(conn, f"ALTER TABLE agent_memory ALTER COLUMN embedding TYPE vector({EMBEDDING_DIMENSIONS})")
    # Index is (re)created afterward.
    assert _ran(conn, "CREATE INDEX IF NOT EXISTS idx_agent_memory_embedding")


@pytest.mark.asyncio
async def test_noop_when_dimension_already_current():
    conn = FakeConn(current_dim=EMBEDDING_DIMENSIONS)
    await ensure_agent_memory_schema(conn)

    assert not _ran(conn, "DROP INDEX")
    assert not _ran(conn, "DELETE FROM agent_memory")
    assert not _ran(conn, "ALTER TABLE agent_memory ALTER COLUMN embedding")


@pytest.mark.asyncio
async def test_noop_on_fresh_db_with_unspecified_dimension():
    # atttypmod is -1 when the column has no declared dimension — never migrate.
    conn = FakeConn(current_dim=-1)
    await ensure_agent_memory_schema(conn)

    assert not _ran(conn, "ALTER TABLE agent_memory ALTER COLUMN embedding")
