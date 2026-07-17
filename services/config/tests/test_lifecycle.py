"""Tests for the canonical experiment lifecycle scheduler."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.experiments import lifecycle


def _experiment(key: str, status: str, version: int = 3) -> dict:
    return {
        "key": key,
        "project_id": "apdl",
        "flag_key": key,
        "status": status,
        "version": version,
    }


@pytest.mark.asyncio
async def test_advance_due_experiments_uses_atomic_transition(monkeypatch):
    pool = object()
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    candidates = [
        _experiment("scheduled", "scheduled"),
        _experiment("expired", "running", version=7),
    ]
    get_due = AsyncMock(return_value=candidates)
    transition = AsyncMock(return_value=({"version": 4}, {"version": 4}))
    monkeypatch.setattr(lifecycle.pg_store, "get_due_experiments", get_due)
    monkeypatch.setattr(
        lifecycle.mutations,
        "transition_due_experiment",
        transition,
    )

    advanced = await lifecycle.advance_due_experiments(pool, now=now)

    assert advanced == 2
    get_due.assert_awaited_once_with(pool, now)


@pytest.mark.asyncio
async def test_advance_due_experiments_counts_only_won_versions(monkeypatch):
    pool = object()
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    candidate = _experiment("checkout", "running")
    monkeypatch.setattr(
        lifecycle.pg_store,
        "get_due_experiments",
        AsyncMock(return_value=[candidate]),
    )
    transition = AsyncMock(return_value=None)
    monkeypatch.setattr(
        lifecycle.mutations,
        "transition_due_experiment",
        transition,
    )

    advanced = await lifecycle.advance_due_experiments(pool, now=now)

    assert advanced == 0
    transition.assert_awaited_once_with(
        pool,
        project_id="apdl",
        key="checkout",
        expected_version=3,
        now=now,
    )


@pytest.mark.asyncio
async def test_advance_due_experiments_isolates_one_failed_candidate(monkeypatch):
    pool = object()
    candidates = [
        _experiment("broken", "scheduled"),
        _experiment("healthy", "running"),
    ]
    monkeypatch.setattr(
        lifecycle.pg_store,
        "get_due_experiments",
        AsyncMock(return_value=candidates),
    )
    transition = AsyncMock(
        side_effect=[RuntimeError("injected"), ({"version": 4}, {"version": 4})]
    )
    monkeypatch.setattr(
        lifecycle.mutations,
        "transition_due_experiment",
        transition,
    )

    assert await lifecycle.advance_due_experiments(pool) == 1


@pytest.mark.parametrize("value", [0, -1, 86_401, True, 1.5])
def test_lifecycle_interval_rejects_non_positive_or_unbounded_values(value):
    with pytest.raises(ValueError, match="between 1 and 86400"):
        lifecycle.validate_interval_seconds(value)


@pytest.mark.parametrize("value", [1, 300, 86_400])
def test_lifecycle_interval_accepts_bounded_positive_values(value):
    assert lifecycle.validate_interval_seconds(value) == value


@pytest.mark.asyncio
async def test_monitor_rejects_invalid_interval_before_sweeping(monkeypatch):
    sweep = AsyncMock()
    monkeypatch.setattr(lifecycle, "advance_due_experiments", sweep)

    with pytest.raises(ValueError, match="between 1 and 86400"):
        await lifecycle.run_lifecycle_monitor(object(), interval_seconds=0)

    sweep.assert_not_awaited()
