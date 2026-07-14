"""Database-owned admission and audit behavior for custom-agent dry-runs."""

from __future__ import annotations

from typing import Any

import pytest

from app.store import custom_agent_tests as store


class _Txn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, fetchvals: list[Any] | None = None) -> None:
        self.fetchvals = list(fetchvals or [])
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> _Txn:
        return _Txn()

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executed.append((query, args))
        return self.fetchvals.pop(0)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(self, fetchvals: list[Any] | None = None) -> None:
        self.conn = _Conn(fetchvals)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


async def _begin(pool: _Pool) -> None:
    await store.begin_custom_agent_test_run(
        pool,
        test_run_id="test-1",
        project_id="demo",
        agent_slug="churn_watch",
        model_tier="fast",
        time_range_days=7,
        max_tool_steps=4,
        allowed_tool_count=1,
        configured_preset_count=2,
    )


@pytest.mark.asyncio
async def test_begin_serializes_project_admission_and_records_cost_envelope():
    pool = _Pool([False, 0])

    await _begin(pool)

    queries = [query for query, _ in pool.conn.executed]
    assert "pg_advisory_xact_lock" in queries[0]
    assert "lease_expires_at <= now()" in queries[1]
    assert "SELECT EXISTS" in queries[2]
    assert "started_at >= now() - make_interval" in queries[3]
    assert "INSERT INTO custom_agent_test_runs" in queries[4]
    assert pool.conn.executed[4][1][:8] == (
        "test-1",
        "demo",
        "churn_watch",
        "fast",
        7,
        4,
        1,
        2,
    )
    assert store.CUSTOM_AGENT_TEST_LEASE_SECONDS >= (17 * 4 * 120) + 3600


@pytest.mark.asyncio
async def test_begin_rejects_a_concurrent_project_run_before_rate_check():
    pool = _Pool([True])

    with pytest.raises(store.CustomAgentTestBusyError):
        await _begin(pool)

    assert not any(
        "INSERT INTO custom_agent_test_runs" in query
        for query, _ in pool.conn.executed
    )


@pytest.mark.asyncio
async def test_begin_enforces_the_rolling_project_rate_limit():
    pool = _Pool([False, store.CUSTOM_AGENT_TEST_RATE_LIMIT])

    with pytest.raises(store.CustomAgentTestRateLimitError):
        await _begin(pool)

    assert not any(
        "INSERT INTO custom_agent_test_runs" in query
        for query, _ in pool.conn.executed
    )


@pytest.mark.asyncio
async def test_finish_terminalizes_the_audit_row_with_actual_spend():
    pool = _Pool()

    await store.finish_custom_agent_test_run(
        pool,
        "test-1",
        status="succeeded",
        preset_tool_calls=2,
        agentic_tool_calls=3,
        llm_calls=4,
        llm_latency_ms=450,
        total_latency_ms=510,
    )

    query, args = pool.conn.executed[0]
    assert "UPDATE custom_agent_test_runs" in query
    assert "WHERE test_run_id = $1 AND status = 'running'" in query
    assert args == ("test-1", "succeeded", 2, 3, 4, 450, 510, None)
