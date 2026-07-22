import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app import main


@pytest.mark.asyncio
async def test_credential_bearing_redis_url_is_redacted_from_startup_logs(
    caplog,
    monkeypatch,
):
    redis_url = (
        "rediss://redis-user:redis-password@cache.internal:6380/4"
        "?token=query-secret"
    )
    redis = SimpleNamespace(aclose=AsyncMock())
    maintenance_connection = SimpleNamespace(
        add_termination_listener=MagicMock(),
        remove_termination_listener=MagicMock(),
        fetchval=AsyncMock(return_value=True),
        close=AsyncMock(),
    )
    pool = SimpleNamespace(
        acquire=AsyncMock(return_value=maintenance_connection),
        close=AsyncMock(),
    )
    from_url = MagicMock(return_value=redis)
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://local.test/apdl")
    monkeypatch.setattr(main.aioredis, "from_url", from_url)
    monkeypatch.setattr(main.asyncpg, "create_pool", create_pool)

    with caplog.at_level(logging.INFO, logger="app.main"):
        async with main.lifespan(FastAPI()):
            pass

    log_output = "\n".join(caplog.messages)
    assert "cache.internal" in log_output
    assert "port=6380" in log_output
    assert "db=4" in log_output
    assert "tls=enabled" in log_output
    assert "redis-user" not in log_output
    assert "redis-password" not in log_output
    assert "query-secret" not in log_output
    from_url.assert_called_once_with(redis_url)
    create_pool.assert_awaited_once_with(
        "postgresql://local.test/apdl",
        min_size=1,
        max_size=5,
        init=main._acquire_maintenance_inhibitor,
        reset=main._reset_maintenance_inhibitor,
        max_inactive_connection_lifetime=0,
    )
    redis.aclose.assert_awaited_once()
    pool.acquire.assert_awaited_once()
    maintenance_connection.add_termination_listener.assert_called_once()
    maintenance_connection.fetchval.assert_awaited_once()
    heartbeat_query, primary_lock_id, guard_lock_id = (
        maintenance_connection.fetchval.await_args.args
    )
    assert "objsubid = 1" in heartbeat_query
    assert primary_lock_id == main.MAINTENANCE_INHIBITOR_LOCK_ID
    assert guard_lock_id == main.MAINTENANCE_GUARD_LOCK_ID
    maintenance_connection.remove_termination_listener.assert_called_once()
    maintenance_connection.close.assert_awaited_once()
    pool.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_pool_reset_restores_the_maintenance_inhibitor() -> None:
    connection = SimpleNamespace(
        get_reset_query=lambda: "SELECT pg_advisory_unlock_all()",
        execute=AsyncMock(),
    )

    await main._reset_maintenance_inhibitor(connection)

    assert [call.args for call in connection.execute.await_args_list] == [
        ("SELECT pg_advisory_unlock_all()",),
        ("SELECT pg_advisory_lock_shared($1)", main.MAINTENANCE_INHIBITOR_LOCK_ID),
        ("SELECT pg_advisory_lock_shared($1)", main.MAINTENANCE_GUARD_LOCK_ID),
    ]


@pytest.mark.asyncio
async def test_monitor_start_proves_locks_before_returning() -> None:
    connection = SimpleNamespace(
        add_termination_listener=MagicMock(),
        remove_termination_listener=MagicMock(),
        fetchval=AsyncMock(return_value=True),
        close=AsyncMock(),
    )

    task, listener = await main._start_maintenance_monitor(connection)

    connection.fetchval.assert_awaited_once()
    await main._close_maintenance_monitor(connection, task, listener)


@pytest.mark.asyncio
async def test_monitor_start_failure_removes_listener_without_starting_task() -> None:
    connection = SimpleNamespace(
        add_termination_listener=MagicMock(),
        remove_termination_listener=MagicMock(),
        fetchval=AsyncMock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="locks were lost"):
        await main._start_maintenance_monitor(connection)

    connection.add_termination_listener.assert_called_once()
    connection.remove_termination_listener.assert_called_once()


@pytest.mark.asyncio
async def test_dedicated_inhibitor_loss_immediately_aborts_process(monkeypatch) -> None:
    aborted = MagicMock()
    connection_lost = main.asyncio.Event()
    connection_lost.set()
    monkeypatch.setattr(main, "_abort_process_on_maintenance_loss", aborted)

    await main._monitor_maintenance_inhibitor(
        SimpleNamespace(),
        connection_lost,
        heartbeat_seconds=0.001,
        heartbeat_timeout_seconds=0.001,
    )

    aborted.assert_called_once_with()


def test_maintenance_loss_abort_uses_immediate_process_exit(monkeypatch) -> None:
    exit_process = MagicMock()
    monkeypatch.setattr(main.os, "_exit", exit_process)

    main._abort_process_on_maintenance_loss()

    exit_process.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_dedicated_heartbeat_checks_both_exact_lock_ids(monkeypatch) -> None:
    checked = main.asyncio.Event()

    class Connection:
        call = None

        async def fetchval(self, query: str, *args):
            self.call = (query, args)
            checked.set()
            return True

    connection = Connection()
    aborted = MagicMock()
    monkeypatch.setattr(main, "_abort_process_on_maintenance_loss", aborted)
    task = main.asyncio.create_task(
        main._monitor_maintenance_inhibitor(
            connection,
            main.asyncio.Event(),
            heartbeat_seconds=0.001,
            heartbeat_timeout_seconds=0.01,
        )
    )
    await main.asyncio.wait_for(checked.wait(), timeout=0.1)
    task.cancel()
    await main.asyncio.gather(task, return_exceptions=True)

    assert connection.call is not None
    query, args = connection.call
    assert "classid = 0" in query
    assert "objsubid = 1" in query
    assert args == (
        main.MAINTENANCE_INHIBITOR_LOCK_ID,
        main.MAINTENANCE_GUARD_LOCK_ID,
    )
    aborted.assert_not_called()


@pytest.mark.asyncio
async def test_dedicated_inhibitor_heartbeat_is_bounded(monkeypatch) -> None:
    class UnresponsiveConnection:
        async def fetchval(self, _query: str, *_args):
            await main.asyncio.Event().wait()

    aborted = MagicMock()
    monkeypatch.setattr(main, "_abort_process_on_maintenance_loss", aborted)

    await main.asyncio.wait_for(
        main._monitor_maintenance_inhibitor(
            UnresponsiveConnection(),
            main.asyncio.Event(),
            heartbeat_seconds=0.001,
            heartbeat_timeout_seconds=0.001,
        ),
        timeout=0.1,
    )

    aborted.assert_called_once_with()
