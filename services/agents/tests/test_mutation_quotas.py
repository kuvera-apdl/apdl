"""PostgreSQL-backed mutation quota reservation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.store.mutation_quotas import (
    MutationQuotaExceededError,
    MutationQuotaUnavailableError,
    POLICY_VERSION,
    reserve_mutation,
)


@dataclass
class _Backend:
    now: datetime = datetime(2026, 7, 15, tzinfo=UTC)
    rows: dict[tuple[str, str, str], tuple[str, datetime]] = field(
        default_factory=dict
    )
    lock_calls: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def transaction(self) -> _Transaction:
        return _Transaction()

    def _maybe_fail(self) -> None:
        if self.backend.fail:
            raise RuntimeError("postgres unavailable")

    async def execute(self, query: str, *args: Any) -> str:
        self._maybe_fail()
        if "pg_advisory_xact_lock" in query:
            self.backend.lock_calls.append((str(args[0]), str(args[1])))
            return "SELECT 1"
        if "INSERT INTO agent_mutation_quota_reservations" in query:
            project_id, action_type, idempotency_key, policy_version = map(str, args)
            key = (project_id, action_type, idempotency_key)
            if key in self.backend.rows:
                return "INSERT 0 0"
            self.backend.rows[key] = (policy_version, self.backend.now)
            return "INSERT 0 1"
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._maybe_fail()
        if "SELECT policy_version" in query:
            row = self.backend.rows.get((str(args[0]), str(args[1]), str(args[2])))
            return row[0] if row is not None else None
        if "SELECT count(*)" in query:
            project_id, action_type, policy_version = map(str, args[:3])
            cutoff = self.backend.now - timedelta(seconds=int(args[3]))
            return sum(
                1
                for (row_project, row_action, _), (row_policy, occurred_at)
                in self.backend.rows.items()
                if row_project == project_id
                and row_action == action_type
                and row_policy == policy_version
                and occurred_at >= cutoff
            )
        raise AssertionError(query)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(self, backend: _Backend) -> None:
        self.conn = _Conn(backend)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


async def _reserve(pool: _Pool, project_id: str, key: str):
    return await reserve_mutation(
        pool,
        project_id=project_id,
        action_type="feature_proposal",
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_same_idempotency_key_reuses_one_reservation() -> None:
    backend = _Backend()
    pool = _Pool(backend)

    first = await _reserve(pool, "projectA", "run-1:proposal:p1")
    retry = await _reserve(pool, "projectA", "run-1:proposal:p1")

    assert first.already_reserved is False
    assert retry.already_reserved is True
    assert first.used == retry.used == 1
    assert len(backend.rows) == 1
    assert backend.lock_calls == [
        ("projectA", "feature_proposal"),
        ("projectA", "feature_proposal"),
    ]


@pytest.mark.asyncio
async def test_same_key_and_action_are_isolated_between_projects() -> None:
    backend = _Backend()
    pool = _Pool(backend)

    first = await _reserve(pool, "projectA", "run-1:proposal:p1")
    second = await _reserve(pool, "projectB", "run-1:proposal:p1")

    assert first.already_reserved is False
    assert second.already_reserved is False
    assert first.used == second.used == 1
    assert len(backend.rows) == 2


@pytest.mark.asyncio
async def test_limit_is_shared_across_store_instances() -> None:
    backend = _Backend()
    replica_a = _Pool(backend)
    replica_b = _Pool(backend)

    assert (await _reserve(replica_a, "projectA", "mutation-1")).used == 1
    assert (await _reserve(replica_b, "projectA", "mutation-2")).used == 2
    assert (await _reserve(replica_a, "projectA", "mutation-3")).used == 3

    with pytest.raises(MutationQuotaExceededError) as raised:
        await _reserve(replica_b, "projectA", "mutation-4")

    assert raised.value.used == raised.value.limit == 3
    assert len(backend.rows) == 3


@pytest.mark.asyncio
async def test_expired_reservations_do_not_consume_the_rolling_hour() -> None:
    backend = _Backend()
    pool = _Pool(backend)
    for index in range(3):
        await _reserve(pool, "projectA", f"old-{index}")

    backend.now += timedelta(hours=1, seconds=1)
    current = await _reserve(pool, "projectA", "current")

    assert current.used == 1
    assert current.policy_version == POLICY_VERSION
    assert len(backend.rows) == 4


@pytest.mark.asyncio
async def test_database_failure_is_fail_closed() -> None:
    backend = _Backend(fail=True)

    with pytest.raises(MutationQuotaUnavailableError) as raised:
        await _reserve(_Pool(backend), "projectA", "mutation-1")

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert backend.rows == {}


@pytest.mark.asyncio
async def test_blank_idempotency_key_is_rejected_before_database_access() -> None:
    backend = _Backend(fail=True)

    with pytest.raises(ValueError, match="not blank"):
        await _reserve(_Pool(backend), "projectA", "   ")

    assert backend.lock_calls == []
