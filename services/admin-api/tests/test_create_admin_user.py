"""Operator CLI provenance checks for Agents and Codegen execution roles."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import create_admin_user


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.in_transaction = True
        self.connection.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc, traceback
        self.connection.in_transaction = False
        self.connection.transaction_exit_type = exc_type
        return False


class FakeConnection:
    def __init__(
        self,
        *,
        created_by: UUID | None,
        execution_authorized: bool,
        user_id: UUID | None = None,
    ) -> None:
        self.project = {
            "created_by": created_by,
            "execution_authorized": execution_authorized,
        }
        self.user_id = user_id
        self.calls: list[tuple[str, tuple[object, ...], bool]] = []
        self.in_transaction = False
        self.transaction_entries = 0
        self.transaction_exit_type = None
        self.closed = False

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return "OK"

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return self.project

    async def fetchval(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return self.user_id

    async def close(self) -> None:
        self.closed = True


def _args(**overrides):
    values = {
        "email": "Operator@Example.com",
        "project_id": "demo",
        "roles": ["agents:manage"],
        "password_stdin": False,
        "allow_self_registered_execution": False,
        "override_actor": None,
        "override_reason": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _sql_calls(connection: FakeConnection) -> list[str]:
    return [" ".join(query.split()) for query, _, _ in connection.calls]


def _patch_runtime(monkeypatch, connection: FakeConnection) -> None:
    async def connect(_dsn: str):
        return connection

    monkeypatch.setattr(create_admin_user.asyncpg, "connect", connect)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://operator-test")
    monkeypatch.setattr(create_admin_user.getpass, "getpass", lambda: "password")
    monkeypatch.setattr(
        create_admin_user,
        "hash_password",
        lambda password: f"$argon2id${password}",
    )


@pytest.mark.asyncio
async def test_self_registered_project_rejects_execution_role_without_override(
    monkeypatch,
):
    connection = FakeConnection(
        created_by=UUID("10000000-0000-4000-8000-000000000001"),
        execution_authorized=False,
    )
    _patch_runtime(monkeypatch, connection)

    with pytest.raises(SystemExit, match="Self-registered projects require"):
        await create_admin_user.provision(_args())

    sql = _sql_calls(connection)
    assert not any(
        "INSERT INTO admin_project_execution_authorizations" in query
        for query in sql
    )
    assert not any("INSERT INTO admin_user_projects" in query for query in sql)
    assert connection.transaction_exit_type is SystemExit
    assert connection.closed is True


@pytest.mark.asyncio
async def test_explicit_override_is_audited_before_role_grant_in_one_transaction(
    monkeypatch,
):
    connection = FakeConnection(
        created_by=UUID("10000000-0000-4000-8000-000000000001"),
        execution_authorized=False,
    )
    _patch_runtime(monkeypatch, connection)

    await create_admin_user.provision(
        _args(
            allow_self_registered_execution=True,
            override_actor="operator@example.com",
            override_reason="Approved for production experiment automation",
        )
    )

    sql = _sql_calls(connection)
    override_index = next(
        index
        for index, query in enumerate(sql)
        if "INSERT INTO admin_project_execution_authorizations" in query
    )
    membership_index = next(
        index
        for index, query in enumerate(sql)
        if "INSERT INTO admin_user_projects" in query
    )
    assert override_index < membership_index
    override_call = connection.calls[override_index]
    assert override_call[1] == (
        "demo",
        "operator@example.com",
        "Approved for production experiment automation",
    )
    maintenance_calls = connection.calls[:2]
    transaction_calls = connection.calls[2:]
    assert all("pg_advisory_lock_shared" in call[0] for call in maintenance_calls)
    assert [call[1] for call in maintenance_calls] == [
        (create_admin_user.MAINTENANCE_INHIBITOR_LOCK_ID,),
        (create_admin_user.MAINTENANCE_GUARD_LOCK_ID,),
    ]
    assert all(call[2] is False for call in maintenance_calls)
    assert all(in_transaction for _, _, in_transaction in transaction_calls)
    assert connection.transaction_entries == 1
    assert connection.transaction_exit_type is None
    assert connection.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created_by", "execution_authorized"),
    [
        (None, True),
        (UUID("10000000-0000-4000-8000-000000000001"), True),
    ],
)
async def test_existing_execution_authority_allows_role_grant_without_override(
    monkeypatch,
    created_by,
    execution_authorized,
):
    connection = FakeConnection(
        created_by=created_by,
        execution_authorized=execution_authorized,
    )
    _patch_runtime(monkeypatch, connection)

    await create_admin_user.provision(_args())

    sql = _sql_calls(connection)
    assert any("INSERT INTO admin_user_projects" in query for query in sql)
    assert not any(
        "INSERT INTO admin_project_execution_authorizations" in query
        for query in sql
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args, message",
    [
        (
            _args(
                allow_self_registered_execution=True,
                override_reason="approved",
            ),
            "--override-actor is required",
        ),
        (
            _args(
                roles=["agents:read"],
                allow_self_registered_execution=True,
                override_actor="operator@example.com",
                override_reason="approved",
            ),
            "requires an Agents execution role",
        ),
        (
            _args(override_actor="operator@example.com"),
            "require --allow-self-registered-execution",
        ),
    ],
)
async def test_override_flags_are_strict_and_validated_before_connect(
    monkeypatch,
    args,
    message,
):
    async def unexpected_connect(_dsn: str):
        raise AssertionError("invalid CLI arguments must not open PostgreSQL")

    monkeypatch.setattr(create_admin_user.asyncpg, "connect", unexpected_connect)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://operator-test")
    monkeypatch.setattr(create_admin_user.getpass, "getpass", lambda: "password")

    with pytest.raises(SystemExit, match=message):
        await create_admin_user.provision(args)


@pytest.mark.asyncio
async def test_missing_postgres_url_fails_before_prompt_hash_or_connect(monkeypatch):
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    def unexpected_prompt():
        raise AssertionError("missing DSN must fail before prompting")

    def unexpected_hash(_password: str):
        raise AssertionError("missing DSN must fail before hashing")

    async def unexpected_connect(_dsn: str):
        raise AssertionError("missing DSN must fail before connecting")

    monkeypatch.setattr(create_admin_user.getpass, "getpass", unexpected_prompt)
    monkeypatch.setattr(create_admin_user, "hash_password", unexpected_hash)
    monkeypatch.setattr(create_admin_user.asyncpg, "connect", unexpected_connect)

    with pytest.raises(SystemExit, match="POSTGRES_URL is required"):
        await create_admin_user.provision(_args())


def test_container_and_make_target_ship_the_operator_cli() -> None:
    repository = Path(__file__).resolve().parents[3]
    dockerfile = (repository / "services/admin-api/Dockerfile").read_text(
        encoding="utf-8"
    )
    makefile = (repository / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("create-admin-user:", 1)[1].split("\n\n", 1)[0]

    assert "COPY scripts/create_admin_user.py scripts/create_admin_user.py" in dockerfile
    assert "$(COMPOSE) run --rm --build --no-deps admin-api" in target
    assert "python scripts/create_admin_user.py $(ARGS)" in target
    assert ".venv/bin/python" not in target


@pytest.mark.asyncio
async def test_cli_can_grant_human_only_credential_management_role(monkeypatch):
    connection = FakeConnection(
        created_by=UUID("10000000-0000-4000-8000-000000000001"),
        execution_authorized=False,
    )
    _patch_runtime(monkeypatch, connection)

    await create_admin_user.provision(_args(roles=["credentials:manage"]))

    membership_call = next(
        call
        for call in connection.calls
        if "INSERT INTO admin_user_projects" in call[0]
    )
    assert membership_call[1][2] == ["credentials:manage"]
    assert not any(
        "INSERT INTO admin_project_execution_authorizations" in query
        for query, _, _ in connection.calls
    )
