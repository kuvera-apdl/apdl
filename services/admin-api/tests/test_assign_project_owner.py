from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import assign_project_owner

OWNER_ID = UUID("20000000-0000-4000-8000-000000000002")


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.in_transaction = True

    async def __aexit__(self, exc_type, exc, traceback):
        del exc, traceback
        self.connection.in_transaction = False
        self.connection.exit_type = exc_type
        return False


class FakeConnection:
    def __init__(self, *, eligible: bool = True) -> None:
        self.eligible = eligible
        self.fetch_count = 0
        self.calls: list[tuple[str, tuple[object, ...], bool]] = []
        self.in_transaction = False
        self.exit_type = None
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return "OK"

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        self.fetch_count += 1
        if self.fetch_count == 1:
            return {"owner_user_id": None}
        if not self.eligible:
            return None
        return {
            "user_id": OWNER_ID,
            "active": True,
            "roles": ["config:read", "members:manage"],
        }

    async def close(self) -> None:
        self.closed = True


def _args(**overrides):
    values = {
        "project_id": "demo",
        "owner_email": "Owner@Example.com",
        "actor": "operator@example.com",
        "reason": "Initial operator-managed project handoff",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch(monkeypatch, connection: FakeConnection) -> None:
    async def connect(_dsn: str):
        return connection

    monkeypatch.setattr(assign_project_owner.asyncpg, "connect", connect)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://operator-test")


@pytest.mark.asyncio
async def test_operator_assignment_locks_validates_and_audits(monkeypatch) -> None:
    connection = FakeConnection()
    _patch(monkeypatch, connection)

    await assign_project_owner.assign_owner(_args())

    sql = [" ".join(query.split()) for query, _, _ in connection.calls]
    assert "FOR UPDATE" in sql[2]
    assert "FOR UPDATE OF account, membership" in sql[3]
    update_index = next(
        index for index, query in enumerate(sql) if "UPDATE admin_projects" in query
    )
    audit_index = next(
        index
        for index, query in enumerate(sql)
        if "INSERT INTO admin_project_ownership_audit" in query
    )
    assert update_index < audit_index
    assert connection.calls[audit_index][1][4:] == (
        "operator@example.com",
        "Initial operator-managed project handoff",
    )
    assert all(
        in_transaction for _, _, in_transaction in connection.calls[2:]
    )
    assert connection.closed is True


@pytest.mark.asyncio
async def test_operator_assignment_rejects_ineligible_target(monkeypatch) -> None:
    connection = FakeConnection(eligible=False)
    _patch(monkeypatch, connection)

    with pytest.raises(SystemExit, match="active project member"):
        await assign_project_owner.assign_owner(_args())

    assert not any(
        "UPDATE admin_projects" in query for query, _, _ in connection.calls
    )
    assert connection.exit_type is SystemExit
    assert connection.closed is True


def test_container_and_make_target_ship_owner_recovery_cli() -> None:
    repository = Path(__file__).resolve().parents[3]
    dockerfile = (repository / "services/admin-api/Dockerfile").read_text(
        encoding="utf-8"
    )
    dockerignore = (
        repository / "services/admin-api/.dockerignore"
    ).read_text(encoding="utf-8")
    makefile = (repository / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("assign-project-owner:", 1)[1].split("\n\n", 1)[0]

    assert "COPY scripts/assign_project_owner.py" in dockerfile
    assert "!scripts/assign_project_owner.py" in dockerignore
    assert "python -m scripts.assign_project_owner $(ARGS)" in target
