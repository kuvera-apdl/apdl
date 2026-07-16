"""Authoritative audit writes fail closed; telemetry is explicitly best effort."""

from __future__ import annotations

import pytest

from app.safety.audit import AuditLogger


class _Acquire:
    async def __aenter__(self):
        raise RuntimeError("audit unavailable")

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def acquire(self) -> _Acquire:
        return _Acquire()


@pytest.mark.asyncio
async def test_required_audit_failure_is_raised() -> None:
    audit = AuditLogger(_Pool())
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await audit.log_required("run-1", "mutation_authorized")


@pytest.mark.asyncio
async def test_best_effort_audit_failure_is_only_for_telemetry() -> None:
    audit = AuditLogger(_Pool())
    assert await audit.log("run-1", "supervisor_heartbeat") == -1
