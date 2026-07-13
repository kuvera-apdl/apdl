"""Tests for the trusted local GitHub repository grant command."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.github import grant_cli
from app.github.app_auth import DiscoveredRepositoryTarget


class FakeAcquire:
    def __init__(self, conn: object):
        self.conn = conn

    async def __aenter__(self) -> object:
        return self.conn

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    def __init__(self, conn: object):
        self.conn = conn
        self.closed = False

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_operator_grant_validates_schema_then_activates_discovered_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = object()
    pool = FakePool(conn)
    events: list[object] = []
    discovered = DiscoveredRepositoryTarget(
        installation_id=123,
        repository_id=456,
        repository_full_name="acme/widget",
        default_branch="trunk",
    )
    connection = SimpleNamespace(
        grant_id="ghg_test",
        project_id="demo",
        repository_id=456,
        repository_full_name="acme/widget",
    )

    async def create_pool(**kwargs: object) -> FakePool:
        events.append(("pool", kwargs))
        return pool

    async def validate_schema(actual_conn: object) -> None:
        assert actual_conn is conn
        events.append("schema")

    async def discover(repository: str) -> DiscoveredRepositoryTarget:
        events.append(("discover", repository))
        return discovered

    async def activate(actual_pool: object, **kwargs: object) -> object:
        assert actual_pool is pool
        events.append(("activate", kwargs))
        return connection

    monkeypatch.setattr(grant_cli.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(grant_cli, "postgres_url", lambda: "postgresql://test")
    monkeypatch.setattr(grant_cli, "assert_schema_ready", validate_schema)
    monkeypatch.setattr(grant_cli, "resolve_repository_target", discover)
    monkeypatch.setattr(grant_cli, "activate_operator_grant", activate)

    result = await grant_cli.activate_repository_grant(
        project_id="demo",
        repository="acme/widget",
        authorized_by="operator@example.com",
    )

    assert result is connection
    assert events == [
        (
            "pool",
            {
                "dsn": "postgresql://test",
                "min_size": 1,
                "max_size": 2,
            },
        ),
        "schema",
        ("discover", "acme/widget"),
        (
            "activate",
            {
                "project_id": "demo",
                "installation_id": 123,
                "repository_id": 456,
                "repository_full_name": "acme/widget",
                "default_base_branch": "trunk",
                "authorization_subject": "operator@example.com",
            },
        ),
    ]
    assert pool.closed is True


@pytest.mark.asyncio
async def test_operator_grant_fails_before_discovery_when_schema_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(object())

    async def create_pool(**kwargs: object) -> FakePool:
        return pool

    async def reject_schema(conn: object) -> None:
        raise RuntimeError("migration 009 is not applied")

    async def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("repository discovery/store must not run")

    monkeypatch.setattr(grant_cli.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(grant_cli, "assert_schema_ready", reject_schema)
    monkeypatch.setattr(grant_cli, "resolve_repository_target", unexpected)
    monkeypatch.setattr(grant_cli, "activate_operator_grant", unexpected)

    with pytest.raises(RuntimeError, match="migration 009"):
        await grant_cli.activate_repository_grant(
            project_id="demo",
            repository="acme/widget",
            authorized_by="operator@example.com",
        )

    assert pool.closed is True


@pytest.mark.asyncio
async def test_operator_revocation_validates_schema_and_exact_project_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = object()
    pool = FakePool(conn)
    events: list[object] = []

    async def create_pool(**kwargs: object) -> FakePool:
        events.append(("pool", kwargs))
        return pool

    async def validate_schema(actual_conn: object) -> None:
        assert actual_conn is conn
        events.append("schema")

    async def revoke(actual_pool: object, **kwargs: object) -> bool:
        assert actual_pool is pool
        events.append(("revoke", kwargs))
        return True

    monkeypatch.setattr(grant_cli.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(grant_cli, "postgres_url", lambda: "postgresql://test")
    monkeypatch.setattr(grant_cli, "assert_schema_ready", validate_schema)
    monkeypatch.setattr(grant_cli, "revoke_stored_repository_grant", revoke)

    await grant_cli.revoke_repository_grant(
        project_id="demo",
        grant_id="ghg_verifiedgrant",
    )

    assert events == [
        (
            "pool",
            {"dsn": "postgresql://test", "min_size": 1, "max_size": 2},
        ),
        "schema",
        (
            "revoke",
            {"project_id": "demo", "grant_id": "ghg_verifiedgrant"},
        ),
    ]
    assert pool.closed is True


@pytest.mark.asyncio
async def test_operator_revocation_rejects_unknown_or_already_revoked_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool(object())

    async def create_pool(**_kwargs: object) -> FakePool:
        return pool

    async def validate_schema(_conn: object) -> None:
        return None

    async def revoke(_pool: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(grant_cli.asyncpg, "create_pool", create_pool)
    monkeypatch.setattr(grant_cli, "assert_schema_ready", validate_schema)
    monkeypatch.setattr(grant_cli, "revoke_stored_repository_grant", revoke)

    with pytest.raises(RuntimeError, match="Active repository grant was not found"):
        await grant_cli.revoke_repository_grant(
            project_id="demo",
            grant_id="ghg_verifiedgrant",
        )

    assert pool.closed is True


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--project-id", "bad-project"], "project id"),
        (["--authorized-by", "   "], "must not be blank"),
        (["--authorized-by", "operator\nsecond-line"], "single line"),
    ],
)
def test_parser_rejects_invalid_operator_evidence(
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid = [
        "--project-id",
        "demo",
        "--repository",
        "acme/widget",
        "--authorized-by",
        "operator@example.com",
    ]
    flag = argv[0]
    index = valid.index(flag)
    valid[index : index + 2] = argv

    with pytest.raises(SystemExit):
        grant_cli._parser().parse_args(valid)

    assert message in capsys.readouterr().err


def test_revoke_parser_rejects_noncanonical_grant_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        grant_cli._revoke_parser().parse_args(
            ["--project-id", "demo", "--grant-id", "not-a-grant"]
        )

    assert "canonical ghg_ format" in capsys.readouterr().err
