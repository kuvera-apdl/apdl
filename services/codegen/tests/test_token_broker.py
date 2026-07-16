"""Tests for DB-authorized GitHub token leases."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.github.app_auth import (
    CODEGEN_PR_WRITE_PERMISSIONS,
    CODEGEN_READ_PERMISSIONS,
    CODEGEN_WRITE_PERMISSIONS,
    AuthorizedRepositoryTarget,
    InstallationToken,
)
from app.github.token_broker import (
    GitHubTokenBroker,
    RepositoryAuthorizationError,
)
from tests.fakes import FakePool


def _issued_token(value: str = "ghs_scoped") -> InstallationToken:
    return InstallationToken(
        token=value,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


@pytest.mark.parametrize(
    ("lease_name", "expected_permissions"),
    [
        ("read_changeset", CODEGEN_READ_PERMISSIONS),
        ("write_changeset", CODEGEN_WRITE_PERMISSIONS),
        ("pr_write_changeset", CODEGEN_PR_WRITE_PERMISSIONS),
    ],
)
@pytest.mark.asyncio
async def test_changeset_lease_resolves_active_db_target_and_exact_profile(
    lease_name, expected_permissions
):
    pool = FakePool()
    pool.add_connection(
        "demo",
        installation_id=42,
        repository_id=987,
    )
    pool.add_changeset("cs_authorized", "demo")
    issued: list[tuple[AuthorizedRepositoryTarget, dict[str, str]]] = []
    revoked: list[str] = []

    async def issue(target, *, permissions):
        issued.append((target, dict(permissions)))
        return _issued_token()

    async def revoke(token: str) -> None:
        revoked.append(token)

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)
    lease = getattr(broker, lease_name)

    async with lease("cs_authorized") as token:
        assert token == "ghs_scoped"
        assert revoked == []

    assert issued == [
        (
            AuthorizedRepositoryTarget(installation_id=42, repository_id=987),
            dict(expected_permissions),
        )
    ]
    assert revoked == ["ghs_scoped"]


@pytest.mark.asyncio
async def test_changeset_lease_fails_closed_when_grant_was_revoked_before_mint():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_revoked", "demo")
    grant_id = pool.store["connections"]["demo"]["grant_id"]
    pool.store["repository_grants"][grant_id]["status"] = "revoked"
    minted = False

    async def issue(target, *, permissions):
        nonlocal minted
        minted = True
        return _issued_token()

    async def revoke(token: str) -> None:
        raise AssertionError(f"unissued token was revoked: {token}")

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    with pytest.raises(RepositoryAuthorizationError, match="no active"):
        async with broker.write_changeset("cs_revoked"):
            raise AssertionError("revoked authority must never yield a token")

    assert minted is False


@pytest.mark.asyncio
async def test_changeset_lease_revokes_and_never_yields_if_grant_changes_during_mint():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_raced", "demo")
    grant_id = pool.store["connections"]["demo"]["grant_id"]
    revoked_tokens: list[str] = []

    async def issue(target, *, permissions):
        pool.store["repository_grants"][grant_id]["status"] = "revoked"
        return _issued_token("ghs_raced")

    async def revoke(token: str) -> None:
        revoked_tokens.append(token)

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    with pytest.raises(RepositoryAuthorizationError, match="no active"):
        async with broker.write_changeset("cs_raced"):
            raise AssertionError("a token minted across revocation must not be yielded")

    assert revoked_tokens == ["ghs_raced"]


@pytest.mark.asyncio
async def test_write_lease_rejects_token_too_short_for_jit_mutation_and_revokes_it():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_short_ttl", "demo")
    revoked: list[str] = []

    async def issue(target, *, permissions):
        return InstallationToken(
            token="ghs_short",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
        )

    async def revoke(token: str) -> None:
        revoked.append(token)

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    with pytest.raises(RepositoryAuthorizationError, match="expires before"):
        async with broker.write_changeset("cs_short_ttl"):
            raise AssertionError("short-lived write token must not be yielded")

    assert revoked == ["ghs_short"]


@pytest.mark.asyncio
async def test_project_read_lease_returns_active_connection_and_revokes_on_error():
    pool = FakePool()
    pool.add_connection(
        "demo",
        repo="acme/widgets",
        installation_id=42,
        repository_id=987,
    )
    revoked: list[str] = []

    async def issue(target, *, permissions):
        assert target == AuthorizedRepositoryTarget(42, 987)
        assert permissions == CODEGEN_READ_PERMISSIONS
        return _issued_token("ghs_project")

    async def revoke(token: str) -> None:
        revoked.append(token)

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    with pytest.raises(LookupError, match="consumer failed"):
        async with broker.read_project("demo") as (connection, token):
            assert connection.repository_full_name == "acme/widgets"
            assert token == "ghs_project"
            raise LookupError("consumer failed")

    assert revoked == ["ghs_project"]


@pytest.mark.asyncio
async def test_revocation_cleanup_failure_does_not_replace_completed_operation(caplog):
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_cleanup", "demo")

    async def issue(target, *, permissions):
        return _issued_token("ghs_cleanup")

    async def revoke(token: str) -> None:
        raise RuntimeError("GitHub cleanup unavailable")

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    async with broker.write_changeset("cs_cleanup") as token:
        assert token == "ghs_cleanup"

    assert "Could not revoke leased GitHub installation token" in caplog.text


@pytest.mark.asyncio
async def test_grant_notification_revokes_an_active_lease_without_double_cleanup():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_active", "demo")
    grant_id = pool.store["connections"]["demo"]["grant_id"]
    revoked = asyncio.Event()
    revoked_tokens: list[str] = []

    async def issue(target, *, permissions):
        return _issued_token("ghs_active")

    async def revoke(token: str) -> None:
        revoked_tokens.append(token)
        revoked.set()

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    async with broker.write_changeset("cs_active") as token:
        assert token == "ghs_active"
        broker._on_grant_revoked(
            None,  # type: ignore[arg-type]
            1,
            "codegen_repository_grant_revoked",
            grant_id,
        )
        await asyncio.wait_for(revoked.wait(), timeout=1)

    assert revoked_tokens == ["ghs_active"]


@pytest.mark.asyncio
async def test_cancellation_waits_for_bounded_token_cleanup():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_cancel", "demo")
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def issue(target, *, permissions):
        return _issued_token("ghs_cancel")

    async def revoke(token: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    broker = GitHubTokenBroker(pool, issue_token=issue, revoke_token=revoke)

    async def consume() -> None:
        async with broker.write_changeset("cs_cancel"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_listener_lifecycle_uses_one_dedicated_pool_connection():
    calls: list[tuple[str, object]] = []

    class _ListenerConnection:
        async def add_listener(self, channel, callback):
            calls.append(("add", channel))

        async def remove_listener(self, channel, callback):
            calls.append(("remove", channel))

    class _ListenerPool:
        def __init__(self):
            self.connection = _ListenerConnection()

        async def acquire(self):
            calls.append(("acquire", self.connection))
            return self.connection

        async def release(self, connection):
            calls.append(("release", connection))

    pool = _ListenerPool()
    broker = GitHubTokenBroker(pool)  # type: ignore[arg-type]

    await broker.start()
    await broker.start()
    await broker.close()

    assert [name for name, _value in calls] == [
        "acquire",
        "add",
        "remove",
        "release",
    ]
