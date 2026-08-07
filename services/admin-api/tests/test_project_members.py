from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app import members
from app.auth import AdminSession, require_session
from app.login_security import DEVICE_COOKIE
from app.models import PendingProjectInvitation
from app.security import token_hash
from conftest import make_settings

ACTOR_ID = UUID("20000000-0000-4000-8000-000000000002")
TARGET_ID = UUID("30000000-0000-4000-8000-000000000003")
INVITATION_ID = UUID("40000000-0000-4000-8000-000000000004")
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
RAW_TOKEN = "a" * 43


class MemberConnection:
    def __init__(
        self,
        *,
        actor_is_owner: bool = True,
        actor_roles: list[str] | None = None,
        invitation_available: bool = True,
        account_email: str = "invitee@example.com",
        target_roles: list[str] | None = None,
        target_is_owner: bool = False,
        pending_blocked_reason: str | None = None,
    ) -> None:
        self.actor_is_owner = actor_is_owner
        self.actor_roles = actor_roles or [
            "config:read",
            "config:write",
            "members:manage",
        ]
        self.invitation_available = invitation_available
        self.account_email = account_email
        self.target_roles = target_roles or ["config:read"]
        self.target_is_owner = target_is_owner
        self.pending_blocked_reason = pending_blocked_reason
        self.membership_exists = False
        self.invitation_row: dict[str, object] | None = None
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.audit_calls: list[tuple[object, ...]] = []
        self.rate_bucket_attempts: dict[tuple[str, str], int] = {}
        self.rate_bucket_started_at: dict[tuple[str, str], datetime] = {}

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query: str, *args):
        self.statements.append((query, args))
        if "INSERT INTO admin_login_rate_buckets" in query:
            scope, key_hash, _, now = args
            key = (str(scope), str(key_hash))
            self.rate_bucket_attempts[key] = (
                self.rate_bucket_attempts.get(key, 0) + 1
            )
            self.rate_bucket_started_at.setdefault(key, now)
            return {
                "window_started_at": self.rate_bucket_started_at[key],
                "attempt_count": self.rate_bucket_attempts[key],
            }
        if "AS is_owner" in query and "SELECT" in query:
            if "membership.created_at AS joined_at" in query:
                return {
                    "user_id": TARGET_ID,
                    "email": "target@example.com",
                    "roles": self.target_roles,
                    "active": True,
                    "is_owner": self.target_is_owner,
                    "joined_at": NOW,
                }
            if "membership.user_id," in query:
                return {
                    "user_id": TARGET_ID,
                    "email": "target@example.com",
                    "roles": self.target_roles,
                    "is_owner": self.target_is_owner,
                }
            return {
                "roles": self.actor_roles,
                "owner_user_id": ACTOR_ID if self.actor_is_owner else TARGET_ID,
                "is_owner": self.actor_is_owner,
            }
        if "expires_at <= NOW()" in query:
            return None
        if "INSERT INTO admin_project_invitations" in query:
            self.invitation_row = {
                "invitation_id": args[0],
                "email": args[3],
                "roles": args[4],
                "expires_at": NOW + timedelta(days=7),
                "created_at": NOW,
            }
            return self.invitation_row
        if "FROM admin_project_invitations AS invitation" in query:
            if not self.invitation_available:
                return None
            return {
                "invitation_id": INVITATION_ID,
                "project_id": "demo",
                "email": "invitee@example.com",
                "roles": ["config:read"],
                "inviter_user_id": ACTOR_ID,
                "expires_at": NOW + timedelta(days=7),
                "owner_user_id": ACTOR_ID,
            }
        if "FROM admin_users" in query and "WHERE user_id = $1" in query:
            return {
                "user_id": TARGET_ID,
                "email": self.account_email,
                "active": True,
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetchval(self, query: str, *args):
        self.statements.append((query, args))
        if "SELECT membership.user_id" in query:
            return None
        if "SELECT user_id FROM admin_users WHERE email" in query:
            return None
        if "SELECT count(*) FROM admin_users" in query:
            return 1
        if "INSERT INTO admin_user_projects" in query:
            if self.membership_exists:
                return None
            self.membership_exists = True
            return args[0]
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetch(self, query: str, *args):
        self.statements.append((query, args))
        if "membership.created_at AS joined_at" in query:
            return [
                {
                    "user_id": ACTOR_ID,
                    "email": "owner@example.com",
                    "roles": self.actor_roles,
                    "active": True,
                    "is_owner": self.actor_is_owner,
                    "joined_at": NOW,
                }
            ]
        if "FROM admin_project_invitations AS invitation" in query:
            if self.invitation_row is None:
                return []
            return [
                {
                    **self.invitation_row,
                    "inviter_email": "owner@example.com",
                    "blocked_reason": self.pending_blocked_reason,
                }
            ]
        if "FROM admin_project_membership_audit" in query:
            return []
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def execute(self, query: str, *args):
        self.statements.append((query, args))
        if "INSERT INTO admin_project_membership_audit" in query:
            self.audit_calls.append(args)
        if "SET accepted_at = NOW()" in query:
            self.invitation_available = False
        if "SET roles = $3" in query:
            self.target_roles = list(args[2])
        return "OK"


class MemberPool:
    def __init__(self, connection: MemberConnection) -> None:
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _session(
    csrf: str,
    *,
    user_id: UUID = ACTOR_ID,
    email: str = "owner@example.com",
) -> AdminSession:
    return AdminSession(
        session_id="10000000-0000-4000-8000-000000000001",
        token_hash="b" * 64,
        csrf_hash=token_hash(csrf),
        user_id=str(user_id),
        email=email,
        projects={
            "demo": frozenset(
                {"config:read", "config:write", "members:manage"}
            )
        },
    )


def _client(
    connection: MemberConnection,
    *,
    session: AdminSession | None,
    settings_overrides: dict[str, object] | None = None,
) -> TestClient:
    app = FastAPI()
    app.state.settings = make_settings(
        registration_enabled=False,
        **(settings_overrides or {}),
    )
    app.state.pg_pool = MemberPool(connection)
    app.include_router(members.router)
    if session is not None:
        app.dependency_overrides[require_session] = lambda: session
    return TestClient(app)


def test_invite_is_revealed_once_and_list_never_contains_secret_material() -> None:
    csrf = "members-csrf"
    connection = MemberConnection()
    with _client(connection, session=_session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        created = client.post(
            "/api/projects/demo/invitations",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={
                "email": "Invitee@Example.com",
                "roles": ["config:read", "config:write"],
            },
        )
        listed = client.get("/api/projects/demo/members")

    assert created.status_code == 201
    reveal = created.json()
    assert reveal["email"] == "invitee@example.com"
    assert reveal["invitation_url"].startswith(
        "http://admin.test/invitations/"
    )
    insert = next(
        call
        for call in connection.statements
        if "INSERT INTO admin_project_invitations" in call[0]
    )
    stored_digest = insert[1][1]
    revealed_token = reveal["invitation_url"].rsplit("/", 1)[1]
    assert len(stored_digest) == 64
    assert stored_digest == token_hash(revealed_token)
    assert stored_digest not in reveal["invitation_url"]

    assert listed.status_code == 200
    pending = listed.json()["pending_invitations"][0]
    assert set(pending) == {
        "invitation_id",
        "email",
        "roles",
        "inviter_email",
        "status",
        "blocked_reason",
        "expires_at",
        "created_at",
    }
    assert pending["status"] == "valid"
    assert pending["blocked_reason"] is None
    assert "invitation_url" not in pending
    assert "token" not in pending
    assert "token_hash" not in pending


def test_pending_invitation_exposes_live_blocked_authority_state() -> None:
    connection = MemberConnection(
        pending_blocked_reason="inviter_lacks_members_manage"
    )
    connection.invitation_row = {
        "invitation_id": INVITATION_ID,
        "email": "invitee@example.com",
        "roles": ["config:read"],
        "expires_at": NOW + timedelta(days=7),
        "created_at": NOW,
    }
    with _client(connection, session=_session("members-csrf")) as client:
        listed = client.get("/api/projects/demo/members")

    assert listed.status_code == 200
    assert listed.json()["pending_invitations"] == [
        {
            "invitation_id": str(INVITATION_ID),
            "email": "invitee@example.com",
            "roles": ["config:read"],
            "inviter_email": "owner@example.com",
            "status": "blocked",
            "blocked_reason": "inviter_lacks_members_manage",
            "expires_at": "2026-08-06T12:00:00Z",
            "created_at": "2026-07-30T12:00:00Z",
        }
    ]
    invitation_query = next(
        query
        for query, _ in connection.statements
        if "END AS blocked_reason" in query
    )
    assert "LEFT JOIN admin_user_projects AS inviter_membership" in invitation_query
    assert "WHEN NOT inviter.active" in invitation_query
    assert "ANY(inviter_membership.roles)" in invitation_query
    assert "invitation.roles <@ inviter_membership.roles" in invitation_query
    assert "project.owner_user_id IS DISTINCT FROM" in invitation_query
    for reason in (
        "inviter_inactive",
        "inviter_not_project_member",
        "inviter_lacks_members_manage",
        "roles_exceed_inviter_authority",
        "members_manage_requires_owner",
    ):
        assert reason in invitation_query


@pytest.mark.parametrize(
    "status, blocked_reason",
    [
        ("valid", "inviter_inactive"),
        ("blocked", None),
    ],
)
def test_pending_invitation_rejects_incoherent_status(
    status: str,
    blocked_reason: str | None,
) -> None:
    with pytest.raises(ValidationError, match="blocked_reason must be null"):
        PendingProjectInvitation(
            invitation_id=INVITATION_ID,
            email="invitee@example.com",
            roles=["config:read"],
            inviter_email="owner@example.com",
            status=status,
            blocked_reason=blocked_reason,
            expires_at=NOW + timedelta(days=7),
            created_at=NOW,
        )


def test_invitation_roles_are_canonical_and_bounded_by_live_authority() -> None:
    csrf = "members-csrf"
    connection = MemberConnection(
        actor_is_owner=False,
        actor_roles=["config:read", "members:manage"],
    )
    with _client(connection, session=_session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        out_of_order = client.post(
            "/api/projects/demo/invitations",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"email": "invitee@example.com", "roles": ["members:manage", "config:read"]},
        )
        manager_grant = client.post(
            "/api/projects/demo/invitations",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"email": "invitee@example.com", "roles": ["members:manage"]},
        )
        above_ceiling = client.post(
            "/api/projects/demo/invitations",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"email": "invitee@example.com", "roles": ["config:write"]},
        )

    assert out_of_order.status_code == 422
    assert manager_grant.status_code == 403
    assert above_ceiling.status_code == 403
    assert not any(
        "INSERT INTO admin_project_invitations" in query
        for query, _ in connection.statements
    )


def test_existing_matching_account_accepts_once_with_audited_membership() -> None:
    csrf = "members-csrf"
    session = _session(
        csrf,
        user_id=TARGET_ID,
        email="invitee@example.com",
    )
    connection = MemberConnection()
    with _client(connection, session=session) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        accepted = client.post(
            f"/api/invitations/{RAW_TOKEN}/accept",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )
        replayed = client.post(
            f"/api/invitations/{RAW_TOKEN}/accept",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )

    assert accepted.status_code == 200
    assert accepted.json()["projects"] == [
        {"project_id": "demo", "roles": ["config:read"]}
    ]
    assert replayed.status_code == 404
    assert len(connection.audit_calls) == 1
    assert connection.audit_calls[0][2] == "invitation_accept"
    assert any(
        "SET accepted_at = NOW()" in query
        for query, _ in connection.statements
    )


def test_wrong_email_and_invalid_lifecycle_use_same_unavailable_response() -> None:
    csrf = "members-csrf"
    wrong_email = MemberConnection(account_email="other@example.com")
    session = _session(csrf, user_id=TARGET_ID, email="other@example.com")
    with _client(wrong_email, session=session) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        mismatched = client.post(
            f"/api/invitations/{RAW_TOKEN}/accept",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )

    unavailable = MemberConnection(invitation_available=False)
    with _client(unavailable, session=None) as client:
        invalid = client.get(f"/api/invitations/{RAW_TOKEN}")

    assert mismatched.status_code == invalid.status_code == 404
    assert mismatched.json() == invalid.json() == {
        "detail": "Invitation is unavailable"
    }


def test_invitation_rate_limit_does_not_consume_login_buckets() -> None:
    connection = MemberConnection()
    with _client(
        connection,
        session=None,
        settings_overrides={
            "invitation_global_rate_limit": 100,
            "invitation_network_rate_limit": 100,
            "invitation_token_rate_limit": 1,
        },
    ) as client:
        accepted = client.get(f"/api/invitations/{RAW_TOKEN}")
        throttled = client.get(f"/api/invitations/{RAW_TOKEN}")

    assert accepted.status_code == 200
    assert throttled.status_code == 429
    assert throttled.headers["Retry-After"] == "60"
    assert {scope for scope, _ in connection.rate_bucket_attempts} == {
        "invitation_global",
        "invitation_network",
        "invitation_token",
    }


def test_invitation_registration_is_atomic_when_public_registration_is_disabled(
    monkeypatch,
) -> None:
    connection = MemberConnection()
    built_sources = []
    build_login_source = members.build_login_source

    def record_login_source(*args, **kwargs):
        source = build_login_source(*args, **kwargs)
        built_sources.append(source)
        return source

    monkeypatch.setattr(
        members,
        "hash_password",
        lambda password: f"$argon2id${password}",
    )
    monkeypatch.setattr(members, "build_login_source", record_login_source)
    with _client(connection, session=None) as client:
        registered = client.post(
            f"/api/invitations/{RAW_TOKEN}/register",
            headers={"Origin": "http://admin.test"},
            json={"password": "invited-password"},
        )

    assert registered.status_code == 201
    assert registered.json()["email"] == "invitee@example.com"
    assert registered.json()["projects"] == [
        {"project_id": "demo", "roles": ["config:read"]}
    ]
    sql = [" ".join(query.split()) for query, _ in connection.statements]
    required_order = [
        next(i for i, query in enumerate(sql) if "pg_advisory_xact_lock" in query),
        next(i for i, query in enumerate(sql) if "INSERT INTO admin_users" in query),
        next(
            i
            for i, query in enumerate(sql)
            if "INSERT INTO admin_user_projects" in query
        ),
        next(i for i, query in enumerate(sql) if "SET accepted_at = NOW()" in query),
        next(
            i
            for i, query in enumerate(sql)
            if "INSERT INTO admin_project_membership_audit" in query
        ),
        next(i for i, query in enumerate(sql) if "INSERT INTO admin_sessions" in query),
    ]
    assert required_order == sorted(required_order)
    assert "apdl_admin_session" in registered.cookies
    assert len(built_sources) == 1
    assert registered.cookies[DEVICE_COOKIE] == built_sources[0].device_token
    assert len(connection.audit_calls) == 1


def test_role_replacement_and_removal_cannot_mutate_owner_or_delegated_manager() -> None:
    csrf = "members-csrf"
    owner_target = MemberConnection(target_is_owner=True)
    with _client(owner_target, session=_session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        replace_owner = client.put(
            f"/api/projects/demo/members/{TARGET_ID}/roles",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"roles": ["config:read", "config:write"]},
        )
        remove_owner = client.delete(
            f"/api/projects/demo/members/{TARGET_ID}",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )
    assert replace_owner.status_code == remove_owner.status_code == 409

    delegated = MemberConnection(
        actor_is_owner=False,
        actor_roles=["config:read", "members:manage"],
        target_roles=["config:read", "members:manage"],
    )
    with _client(delegated, session=_session(csrf)) as client:
        client.cookies.set("apdl_admin_csrf", csrf, path="/")
        replace_manager = client.put(
            f"/api/projects/demo/members/{TARGET_ID}/roles",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={"roles": ["config:read"]},
        )
        remove_manager = client.delete(
            f"/api/projects/demo/members/{TARGET_ID}",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
        )
    assert replace_manager.status_code == remove_manager.status_code == 403


def test_mutations_require_origin_csrf_and_strict_request_shapes() -> None:
    csrf = "members-csrf"
    connection = MemberConnection()
    with _client(connection, session=_session(csrf)) as client:
        missing_csrf = client.post(
            "/api/projects/demo/invitations",
            headers={"Origin": "http://admin.test"},
            json={"email": "invitee@example.com", "roles": ["config:read"]},
        )
        unknown = client.post(
            "/api/projects/demo/invitations",
            headers={"Origin": "http://admin.test", "X-CSRF-Token": csrf},
            json={
                "email": "invitee@example.com",
                "roles": ["config:read"],
                "expires_in_days": 30,
            },
        )

    assert missing_csrf.status_code == 403
    assert unknown.status_code == 422
    assert connection.statements == []
