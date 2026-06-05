import pytest

from app.store import postgres


@pytest.mark.asyncio
async def test_get_flags_filters_client_visible_modes():
    pool = RecordingPool()

    await postgres.get_flags(pool, "apdl", client_visible_only=True)

    assert "evaluation_mode IN ('client', 'both')" in pool.sql
    assert "client_exposed" not in pool.sql
    assert pool.args == ("apdl",)


class RecordingPool:
    sql: str = ""
    args: tuple = ()

    async def fetch(self, sql: str, *args):
        self.sql = sql
        self.args = args
        return []
