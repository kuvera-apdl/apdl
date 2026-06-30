from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.experiments import expiry


# ---- pure date logic ----

def test_parse_end_date_variants():
    assert expiry.parse_end_date("2026-06-01") == date(2026, 6, 1)
    assert expiry.parse_end_date("2026-06-01T12:00:00+00:00") == date(2026, 6, 1)
    assert expiry.parse_end_date("2026-06-01T12:00:00Z") == date(2026, 6, 1)


def test_parse_end_date_unusable_returns_none():
    for raw in ["", "   ", "not-a-date", None, 12345]:
        assert expiry.parse_end_date(raw) is None


def test_is_expired_is_inclusive_of_end_date():
    today = date(2026, 6, 2)
    assert expiry.is_expired("2026-06-01", today) is True   # day after -> expired
    assert expiry.is_expired("2026-06-02", today) is False  # on end date -> still runs
    assert expiry.is_expired("2026-06-03", today) is False  # future
    assert expiry.is_expired("", today) is False            # no end date -> never


# ---- sweep cascade ----

def _backing_flag():
    return {
        "key": "checkout",
        "project_id": "apdl",
        "name": "checkout",
        "state": "active",
        "enabled": True,
        "version": 3,
        "evaluation_mode": "client",
        "default_variant": "control",
        "variants": [{"key": "control", "weight": 1}, {"key": "treatment", "weight": 1}],
        "rules": [],
        "fallthrough": {"rollout": {"percentage": 100, "bucket_by": "user_id"}},
        "owners": [],
        "guardrails": [],
        "auto_disable": True,
        "archived_at": None,
    }


def _running_experiment(end_date):
    return {
        "key": "checkout",
        "project_id": "apdl",
        "flag_key": "checkout",
        "status": "running",
        "description": "",
        "default_variant": "control",
        "variants_json": '[{"key":"control","weight":1},{"key":"treatment","weight":1}]',
        "targeting_rules_json": "[]",
        "primary_metric_json": "{}",
        "traffic_percentage": 100.0,
        "start_date": "",
        "end_date": end_date,
    }


@pytest.mark.asyncio
async def test_expire_due_completes_and_disables_backing_flag(monkeypatch):
    exp = _running_experiment("2026-06-01")
    disabled_flag = {**_backing_flag(), "state": "disabled", "enabled": False, "version": 4}

    monkeypatch.setattr(expiry.pg_store, "get_running_experiments_with_end_date",
                        AsyncMock(return_value=[exp]))
    update_experiment = AsyncMock(return_value=True)
    monkeypatch.setattr(expiry.pg_store, "update_experiment", update_experiment)
    monkeypatch.setattr(expiry.pg_store, "get_flag", AsyncMock(return_value=_backing_flag()))
    update_flag = AsyncMock(return_value=disabled_flag)
    monkeypatch.setattr(expiry.pg_store, "update_flag", update_flag)
    audit = AsyncMock()
    monkeypatch.setattr(expiry.pg_store, "create_flag_audit_entry", audit)
    monkeypatch.setattr(expiry.redis_cache, "invalidate_flags", AsyncMock())
    monkeypatch.setattr(expiry.redis_cache, "invalidate_experiments", AsyncMock())
    broadcaster = AsyncMock()

    completed = await expiry.expire_due_experiments(
        pool=None, redis=None, broadcaster=broadcaster, today=date(2026, 6, 28)
    )

    assert completed == 1
    # experiment persisted as completed
    assert update_experiment.await_args.args[1]["status"] == "completed"
    # backing flag resynced to disabled/not-enabled
    merged = update_flag.await_args.args[1]
    assert merged["state"] == "disabled"
    assert merged["enabled"] is False
    # audit attributes the change to the system with the expiry reason
    assert audit.await_args.kwargs["actor"] == "system"
    assert audit.await_args.kwargs["reason"] == "experiment_ended"
    # both SSE events emitted
    events = [c.args[1] for c in broadcaster.broadcast.await_args_list]
    assert "flag_update" in events and "experiment_update" in events


@pytest.mark.asyncio
async def test_expire_due_aborts_when_flag_sync_loses_race(monkeypatch):
    # A concurrent flag edit makes update_flag return None (optimistic-version
    # mismatch). The experiment must stay 'running' (not persisted as completed)
    # so the next sweep retries — never a completed experiment with a live flag.
    exp = _running_experiment("2026-06-01")
    monkeypatch.setattr(expiry.pg_store, "get_running_experiments_with_end_date",
                        AsyncMock(return_value=[exp]))
    update_experiment = AsyncMock(return_value=True)
    monkeypatch.setattr(expiry.pg_store, "update_experiment", update_experiment)
    monkeypatch.setattr(expiry.pg_store, "get_flag", AsyncMock(return_value=_backing_flag()))
    monkeypatch.setattr(expiry.pg_store, "update_flag", AsyncMock(return_value=None))
    audit = AsyncMock()
    monkeypatch.setattr(expiry.pg_store, "create_flag_audit_entry", audit)
    monkeypatch.setattr(expiry.redis_cache, "invalidate_flags", AsyncMock())
    monkeypatch.setattr(expiry.redis_cache, "invalidate_experiments", AsyncMock())

    completed = await expiry.expire_due_experiments(
        pool=None, redis=None, broadcaster=AsyncMock(), today=date(2026, 6, 28)
    )

    assert completed == 0
    update_experiment.assert_not_awaited()  # never marked completed
    audit.assert_not_awaited()              # no spurious disable audit


@pytest.mark.asyncio
async def test_expire_due_skips_unexpired(monkeypatch):
    monkeypatch.setattr(expiry.pg_store, "get_running_experiments_with_end_date",
                        AsyncMock(return_value=[_running_experiment("2026-12-31")]))
    update_experiment = AsyncMock(return_value=True)
    monkeypatch.setattr(expiry.pg_store, "update_experiment", update_experiment)

    completed = await expiry.expire_due_experiments(
        pool=None, redis=None, broadcaster=AsyncMock(), today=date(2026, 6, 28)
    )

    assert completed == 0
    update_experiment.assert_not_awaited()
