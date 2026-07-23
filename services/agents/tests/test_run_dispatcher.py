"""Durable run dispatcher and resume-state regression tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.store import run_dispatcher


class _Conn:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        quarantine_result: bool = True,
        quarantine_error: Exception | None = None,
    ) -> None:
        self.rows = rows
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchval_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.quarantine_result = quarantine_result
        self.quarantine_error = quarantine_error

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((query, args))
        return self.rows

    async def fetchval(self, query: str, *args: Any) -> str | None:
        self.fetchval_queries.append((query, args))
        if self.quarantine_error is not None:
            raise self.quarantine_error
        return str(args[0]) if self.quarantine_result else None


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        quarantine_result: bool = True,
        quarantine_error: Exception | None = None,
    ) -> None:
        self.conn = _Conn(
            rows,
            quarantine_result=quarantine_result,
            quarantine_error=quarantine_error,
        )

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _row(
    run_id: str,
    *,
    status: str = "started",
    phase: str = "initializing",
    config: Any = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project_id": "demo",
        "autonomy_level": 2,
        "status": status,
        "phase": phase,
        "config": config
        or json.dumps(
            {
                "analysis_types": ["behavior_analysis"],
                "time_range_days": 7,
            }
        ),
    }


@pytest.mark.asyncio
async def test_fetch_dispatches_new_and_approved_resume_rows() -> None:
    pool = _Pool(
        [
            _row("new"),
            _row(
                "resume",
                status="approved",
                phase="resuming",
                config={
                    "analysis_types": ["code_implementation"],
                    "time_range_days": 14,
                    "target_proposal_id": "p1",
                },
            ),
        ]
    )

    rows = await run_dispatcher.fetch_dispatchable_runs(pool)

    assert [row.run_id for row in rows] == ["new", "resume"]
    assert rows[0].resume is False
    assert rows[0].resume_after_approval is False
    assert rows[1].resume is True
    assert rows[1].resume_after_approval is True
    assert rows[1].target_proposal_id == "p1"
    query, _ = pool.conn.queries[0]
    assert "lease_owner_id IS NULL" in query
    assert "execution_lane_project_id = project_id" in query
    assert "status IN ('started', 'running')" in query
    assert "status IN ('approved', 'rejected')" in query


@pytest.mark.asyncio
async def test_invalid_legacy_config_is_atomically_terminalized_and_audited(
    caplog,
) -> None:
    pool = _Pool([_row("bad", config={"analysis_types": ["behavior_analysis"]})])

    assert await run_dispatcher.fetch_dispatchable_runs(pool) == []
    assert len(pool.conn.fetchval_queries) == 1
    query, args = pool.conn.fetchval_queries[0]
    assert "WITH quarantined AS" in query
    assert "SET status = 'failed'" in query
    assert "phase = 'invalid_config'" in query
    assert "execution_lane_project_id = $2" in query
    assert "config IS NOT DISTINCT FROM $3::jsonb" in query
    assert "INSERT INTO agent_audit_log" in query
    assert "run_dispatch_invalid_config" in query
    assert args[:3] == (
        "bad",
        "demo",
        '{"analysis_types":["behavior_analysis"]}',
    )
    audit_config = json.loads(args[3])
    assert audit_config["terminal_status"] == "failed"
    assert audit_config["terminal_phase"] == "invalid_config"
    assert "time_range_days" in audit_config["error"]
    safety_result = json.loads(args[4])
    assert safety_result["passed"] is False
    assert safety_result["checks"][0]["passed"] is False
    assert args[5].startswith("run-dispatch-invalid-config:")
    assert "time_range_days" in caplog.text
    assert "Quarantined" in caplog.text


@pytest.mark.asyncio
async def test_invalid_config_quarantine_failure_is_not_silently_skipped() -> None:
    pool = _Pool(
        [_row("bad", config={"analysis_types": ["behavior_analysis"]})],
        quarantine_error=RuntimeError("audit unavailable"),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await run_dispatcher.fetch_dispatchable_runs(pool)


@pytest.mark.asyncio
async def test_repeated_poll_does_not_duplicate_local_inflight_run(monkeypatch) -> None:
    pool = _Pool([_row("run-1")])
    release = asyncio.Event()
    calls: list[dict[str, Any]] = []

    async def fake_supervisor(**kwargs: Any) -> None:
        calls.append(kwargs)
        await release.wait()

    monkeypatch.setattr(run_dispatcher, "run_supervisor", fake_supervisor)
    dispatcher = run_dispatcher.RunDispatcher(pool, object())

    assert await dispatcher.poll_once() == ("run-1",)
    await asyncio.sleep(0)
    assert await dispatcher.poll_once() == ()
    assert len(calls) == 1
    assert dispatcher.inflight_run_ids == ("run-1",)

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert dispatcher.inflight_run_ids == ()
    await dispatcher.stop()
