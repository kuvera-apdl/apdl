"""Single-process enforcement for the OSS Config runtime."""

from pathlib import Path

import pytest

from app import main


class LockConn:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.calls = []

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return self.acquired
        if "pg_advisory_unlock" in sql:
            return True
        raise AssertionError(sql)


class LockPool:
    def __init__(self, conn):
        self.conn = conn
        self.released = []

    async def acquire(self):
        return self.conn

    async def release(self, conn):
        self.released.append(conn)


@pytest.mark.asyncio
async def test_config_process_holds_and_releases_database_singleton_lock():
    conn = LockConn(acquired=True)
    pool = LockPool(conn)

    held = await main._acquire_config_lock(pool)
    await main._release_config_lock(pool, held)

    assert held is conn
    assert "pg_try_advisory_lock" in conn.calls[0][0]
    assert "pg_advisory_unlock" in conn.calls[1][0]
    assert pool.released == [conn]


@pytest.mark.asyncio
async def test_second_config_process_fails_startup_and_releases_connection():
    conn = LockConn(acquired=False)
    pool = LockPool(conn)

    with pytest.raises(RuntimeError, match="Another Config process"):
        await main._acquire_config_lock(pool)

    assert pool.released == [conn]


def test_lifecycle_environment_contract_has_no_expiry_or_replica_aliases():
    source = (
        Path(__file__).parents[1] / "app" / "main.py"
    ).read_text(encoding="utf-8")

    assert "EXPERIMENT_LIFECYCLE_ENABLED" in source
    assert "EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS" in source
    assert "EXPERIMENT_EXPIRY_" not in source
    assert "CONFIG_REPLICA_COUNT" not in source


@pytest.mark.parametrize("value", ["0", "-1", "86401", "not-an-integer"])
def test_lifecycle_environment_rejects_invalid_interval_before_task_creation(
    monkeypatch,
    value,
):
    monkeypatch.setenv("EXPERIMENT_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS", value)

    with pytest.raises(ValueError, match="EXPERIMENT_LIFECYCLE_INTERVAL_SECONDS"):
        main._start_lifecycle_monitor(object())


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_sse_environment_rejects_non_finite_or_non_positive_durations(
    monkeypatch,
    value,
):
    monkeypatch.setenv("SSE_SEND_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="SSE_SEND_TIMEOUT_SECONDS"):
        main._sse_settings_from_environment()
