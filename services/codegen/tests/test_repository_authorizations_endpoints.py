"""Endpoint tests for project-scoped GitHub App repository onboarding."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.github.user_authorization import (
    REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS,
    derive_pkce_verifier,
    pkce_code_challenge,
)
from app.main import app
from app.jobs.repository_authorization_cleanup import (
    run_repository_authorization_cleanup,
)
from app.store import repository_authorizations as authorization_store
from tests.fakes import FakePool


def _configure_github(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_SLUG", "apdl-test")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY_BASE64",
        base64.b64encode(b"test-private-key").decode("ascii"),
    )
    monkeypatch.setenv(
        "GITHUB_APP_CALLBACK_URL",
        "https://admin.example.test/api/github/codegen/callback",
    )


def _github_transport(captured: dict[str, str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            assert request.method == "POST"
            assert request.url.path == "/login/oauth/access_token"
            assert b"client_secret=client-secret" in request.content
            form = httpx.QueryParams(request.content.decode("ascii"))
            captured["code_verifier"] = form["code_verifier"]
            return httpx.Response(
                200,
                json={"access_token": "ghu_ephemeral", "token_type": "bearer"},
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 77, "login": "octocat"})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 42,
                            "app_id": 123,
                            "permissions": dict(
                                REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS
                            ),
                            "suspended_at": None,
                        }
                    ]
                },
            )
        if request.url.path == "/user/installations/42/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 987,
                            "full_name": "acme/widgets",
                            "default_branch": "trunk",
                            "private": True,
                            "archived": False,
                            "disabled": False,
                            "permissions": {"admin": True},
                        }
                    ]
                },
            )
        if request.url.path == "/applications/Iv1.client/token":
            assert request.method == "DELETE"
            assert json.loads(request.content) == {"access_token": "ghu_ephemeral"}
            return httpx.Response(204)
        raise AssertionError(f"Unexpected GitHub request: {request.url}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_human_owner_connects_project_repository_without_operator_gate(
    monkeypatch,
    authorized_codegen_request,
):
    import app.routers.github_authorizations as authorization_router

    _configure_github(monkeypatch)

    async def verified(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        authorization_router,
        "verify_repository_candidate",
        verified,
    )
    actor_user_id = uuid.uuid4()
    pool = FakePool()
    pool.add_project_actor("demo", actor_user_id)
    authorized_codegen_request(
        "demo",
        frozenset(),
        actor_user_id=str(actor_user_id),
        execution_authorized=False,
    )
    oauth_exchange: dict[str, str] = {}
    github_client = httpx.AsyncClient(
        transport=_github_transport(oauth_exchange)
    )
    app.state.pg_pool = pool
    app.state.github_authorization_http_client = github_client

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            started = await client.post(
                "/v1/github/repository-authorizations",
                json={"project_id": "demo"},
            )
            assert started.status_code == 201
            start_body = started.json()
            assert set(start_body) == {
                "schema_version",
                "authorization_id",
                "installation_url",
                "expires_at",
            }
            assert (
                start_body["schema_version"]
                == "github_repository_authorization_start@1"
            )
            installation_url = httpx.URL(start_body["installation_url"])
            assert installation_url.host == "github.com"
            setup_state = installation_url.params["state"]

            setup = await client.get(
                "/github/repository-authorization/callback",
                params={
                    "state": setup_state,
                    "installation_id": "999999",
                    "setup_action": "install",
                },
            )
            assert setup.status_code == 303
            oauth_url = httpx.URL(setup.headers["location"])
            assert oauth_url.path == "/login/oauth/authorize"
            oauth_state = oauth_url.params["state"]
            assert oauth_url.params["code_challenge_method"] == "S256"
            expected_verifier = derive_pkce_verifier(
                oauth_state,
                "client-secret",
            )
            assert oauth_url.params["code_challenge"] == pkce_code_challenge(
                expected_verifier
            )

            replayed_setup = await client.get(
                "/github/repository-authorization/callback",
                params={
                    "state": setup_state,
                    "installation_id": "42",
                    "setup_action": "install",
                },
            )
            assert replayed_setup.headers["location"] == (
                "/codegen?github_repository_error=authorization_failed"
            )

            callback = await client.get(
                "/github/repository-authorization/callback",
                params={"state": oauth_state, "code": "oauth-code"},
            )
            authorization_id = start_body["authorization_id"]
            assert callback.status_code == 303
            assert callback.headers["location"] == (
                "/codegen?github_repository_authorization="
                + authorization_id
                + "&github_repository_project_id=demo"
            )
            assert oauth_exchange["code_verifier"] == expected_verifier

            replayed_oauth = await client.get(
                "/github/repository-authorization/callback",
                params={"state": oauth_state, "code": "oauth-code"},
            )
            assert replayed_oauth.headers["location"] == (
                "/codegen?github_repository_error=authorization_failed"
            )

            status_response = await client.get(
                f"/v1/github/repository-authorizations/{authorization_id}",
                params={"project_id": "demo"},
            )
            assert status_response.status_code == 200
            status_body = status_response.json()
            assert set(status_body) == {
                "schema_version",
                "authorization_id",
                "project_id",
                "status",
                "repositories",
                "expires_at",
            }
            assert status_body["schema_version"] == (
                "github_repository_authorization@1"
            )
            assert status_body["status"] == "awaiting_selection"
            assert status_body["repositories"] == [
                {
                    "candidate_id": status_body["repositories"][0]["candidate_id"],
                    "repository_id": 987,
                    "repository_full_name": "acme/widgets",
                    "default_base_branch": "trunk",
                    "private": True,
                }
            ]

            completed = await client.post(
                (
                    f"/v1/github/repository-authorizations/{authorization_id}"
                    "/complete"
                ),
                json={
                    "project_id": "demo",
                    "candidate_id": status_body["repositories"][0]["candidate_id"],
                },
            )
            assert completed.status_code == 200
            assert completed.json()["repository_full_name"] == "acme/widgets"
            assert completed.json()["default_base_branch"] == "trunk"
            assert "installation_id" not in completed.json()
    finally:
        del app.state.github_authorization_http_client
        await github_client.aclose()

    grants = list(pool.store["repository_grants"].values())
    assert len(grants) == 1
    assert grants[0]["authorization_source"] == "github_oauth"
    assert grants[0]["authorized_by_user_id"] == actor_user_id
    assert grants[0]["github_user_id"] == 77
    assert pool.store["repository_authorization_candidates"] == {}


@pytest.mark.asyncio
async def test_organization_approval_request_cancels_flow_with_project_status(
    monkeypatch,
    authorized_codegen_request,
):
    _configure_github(monkeypatch)
    actor_user_id = uuid.uuid4()
    pool = FakePool()
    pool.add_project_actor("demo", actor_user_id)
    authorized_codegen_request(
        "demo",
        frozenset(),
        actor_user_id=str(actor_user_id),
        execution_authorized=False,
    )
    app.state.pg_pool = pool

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        started = await client.post(
            "/v1/github/repository-authorizations",
            json={"project_id": "demo"},
        )
        start_body = started.json()
        authorization_id = uuid.UUID(start_body["authorization_id"])
        setup_state = httpx.URL(start_body["installation_url"]).params[
            "state"
        ]

        approval = await client.get(
            "/github/repository-authorization/callback",
            params={"state": setup_state, "setup_action": "request"},
        )

        assert approval.status_code == 303
        assert approval.headers["location"] == (
            "/codegen?github_repository_status=installation_approval_required"
            "&github_repository_project_id=demo"
        )
        assert approval.headers["cache-control"] == "no-store"
        assert approval.headers["pragma"] == "no-cache"
        assert approval.headers["referrer-policy"] == "no-referrer"
        assert authorization_id not in pool.store["repository_authorization_flows"]

        replay = await client.get(
            "/github/repository-authorization/callback",
            params={"state": setup_state, "setup_action": "request"},
        )
        assert replay.headers["location"] == (
            "/codegen?github_repository_error=authorization_failed"
        )

        second = await client.post(
            "/v1/github/repository-authorizations",
            json={"project_id": "demo"},
        )
        second_body = second.json()
        second_id = uuid.UUID(second_body["authorization_id"])
        second_state = httpx.URL(second_body["installation_url"]).params[
            "state"
        ]
        mixed = await client.get(
            "/github/repository-authorization/callback",
            params={
                "state": second_state,
                "setup_action": "request",
                "installation_id": "42",
            },
        )
        assert mixed.headers["location"] == (
            "/codegen?github_repository_error=authorization_failed"
        )
        assert second_id in pool.store["repository_authorization_flows"]


@pytest.mark.asyncio
async def test_completion_rechecks_locked_live_authority(
    monkeypatch,
    authorized_codegen_request,
):
    import app.routers.github_authorizations as authorization_router

    _configure_github(monkeypatch)

    async def verified(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(
        authorization_router,
        "verify_repository_candidate",
        verified,
    )

    actor_user_id = uuid.uuid4()
    authorization_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    pool = FakePool()
    pool.add_project_actor("demo", actor_user_id, owner=False, roles=frozenset())
    pool.store["repository_authorization_flows"][authorization_id] = {
        "authorization_id": authorization_id,
        "project_id": "demo",
        "actor_user_id": actor_user_id,
        "state_hash": "a" * 64,
        "status": "awaiting_selection",
        "github_user_id": 77,
        "github_login": "octocat",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "completed_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    pool.store["repository_authorization_candidates"][candidate_id] = {
        "candidate_id": candidate_id,
        "authorization_id": authorization_id,
        "installation_id": 42,
        "repository_id": 987,
        "repository_full_name": "acme/widgets",
        "default_base_branch": "main",
        "private": True,
        "created_at": datetime.now(timezone.utc),
    }
    authorized_codegen_request("demo", actor_user_id=str(actor_user_id))

    async def stale_outer_check(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(
        authorization_router.store,
        "has_repository_connection_authority",
        stale_outer_check,
    )
    app.state.pg_pool = pool
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/github/repository-authorizations/{authorization_id}/complete",
            json={"project_id": "demo", "candidate_id": str(candidate_id)},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository connection authority was revoked"
    assert pool.store["repository_grants"] == {}
    assert pool.store["connections"] == {}


@pytest.mark.asyncio
async def test_stale_github_candidate_does_not_replace_existing_connection(
    monkeypatch,
    authorized_codegen_request,
):
    import app.routers.github_authorizations as authorization_router

    _configure_github(monkeypatch)
    actor_user_id = uuid.uuid4()
    authorization_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    pool = FakePool()
    pool.add_connection(
        "demo",
        repo="acme/existing",
        repository_id=111,
        grant_id="ghg_existing",
    )
    pool.add_project_actor("demo", actor_user_id)
    pool.store["repository_authorization_flows"][authorization_id] = {
        "authorization_id": authorization_id,
        "project_id": "demo",
        "actor_user_id": actor_user_id,
        "state_hash": "a" * 64,
        "status": "awaiting_selection",
        "github_user_id": 77,
        "github_login": "octocat",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "completed_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    pool.store["repository_authorization_candidates"][candidate_id] = {
        "candidate_id": candidate_id,
        "authorization_id": authorization_id,
        "installation_id": 42,
        "repository_id": 987,
        "repository_full_name": "acme/widgets",
        "default_base_branch": "main",
        "private": True,
        "created_at": datetime.now(timezone.utc),
    }
    authorized_codegen_request("demo", actor_user_id=str(actor_user_id))

    async def stale(*args, **kwargs) -> None:
        raise ValueError("repository default branch changed")

    monkeypatch.setattr(
        authorization_router,
        "verify_repository_candidate",
        stale,
    )
    app.state.pg_pool = pool
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/github/repository-authorizations/{authorization_id}/complete",
            json={"project_id": "demo", "candidate_id": str(candidate_id)},
        )

    assert response.status_code == 409
    assert pool.store["connections"]["demo"]["grant_id"] == "ghg_existing"
    assert pool.store["repository_grants"]["ghg_existing"]["status"] == "active"
    assert pool.store["repository_authorization_flows"][authorization_id][
        "status"
    ] == "awaiting_selection"


@pytest.mark.asyncio
async def test_locked_completion_rejects_candidate_replacement_after_verification():
    actor_user_id = uuid.uuid4()
    authorization_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    pool = FakePool()
    pool.add_connection(
        "demo",
        repo="acme/existing",
        repository_id=111,
        grant_id="ghg_existing",
    )
    pool.add_project_actor("demo", actor_user_id)
    pool.store["repository_authorization_flows"][authorization_id] = {
        "authorization_id": authorization_id,
        "project_id": "demo",
        "actor_user_id": actor_user_id,
        "state_hash": "a" * 64,
        "status": "awaiting_selection",
        "github_user_id": 77,
        "github_login": "octocat",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "completed_at": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    pool.store["repository_authorization_candidates"][candidate_id] = {
        "candidate_id": candidate_id,
        "authorization_id": authorization_id,
        "installation_id": 42,
        "repository_id": 987,
        "repository_full_name": "acme/widgets",
        "default_base_branch": "main",
        "private": True,
        "created_at": datetime.now(timezone.utc),
    }
    verified = await authorization_store.get_completion_candidate(
        pool,
        authorization_id=authorization_id,
        project_id="demo",
        actor_user_id=actor_user_id,
        candidate_id=candidate_id,
    )

    # Model a privileged delete+insert replacement after the GitHub check. The
    # completion transaction must compare its locked row to `verified` before
    # revoking the existing connection.
    pool.store["repository_authorization_candidates"][candidate_id].update(
        installation_id=99,
        repository_id=654,
        repository_full_name="attacker/replacement",
    )
    with pytest.raises(authorization_store.RepositoryAuthorizationConflict):
        await authorization_store.complete_authorization(
            pool,
            authorization_id=authorization_id,
            project_id="demo",
            actor_user_id=actor_user_id,
            candidate_id=candidate_id,
            verified_candidate=verified,
        )

    assert pool.store["connections"]["demo"]["grant_id"] == "ghg_existing"
    assert pool.store["repository_grants"]["ghg_existing"]["status"] == "active"


@pytest.mark.asyncio
async def test_periodic_cleanup_purges_expired_flow_and_private_inventory(
    monkeypatch,
):
    pool = FakePool()
    actor_user_id = uuid.uuid4()
    expired_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    pool.store["repository_authorization_flows"][expired_id] = {
        "authorization_id": expired_id,
        "project_id": "demo",
        "actor_user_id": actor_user_id,
        "state_hash": "a" * 64,
        "status": "completed",
        "github_user_id": 77,
        "github_login": "octocat",
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        "completed_at": datetime.now(timezone.utc) - timedelta(seconds=2),
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=11),
        "updated_at": datetime.now(timezone.utc),
    }
    pool.store["repository_authorization_candidates"][candidate_id] = {
        "candidate_id": candidate_id,
        "authorization_id": expired_id,
        "installation_id": 42,
        "repository_id": 987,
        "repository_full_name": "private/widgets",
        "default_base_branch": "main",
        "private": True,
        "created_at": datetime.now(timezone.utc),
    }

    async def stop_after_first_sweep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_sweep)
    with pytest.raises(asyncio.CancelledError):
        await run_repository_authorization_cleanup(pool, interval_seconds=0)

    assert expired_id not in pool.store["repository_authorization_flows"]
    assert candidate_id not in pool.store["repository_authorization_candidates"]


@pytest.mark.asyncio
async def test_expired_status_remains_gone_and_physically_purges_private_inventory(
    authorized_codegen_request,
):
    actor_user_id = uuid.uuid4()
    authorization_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    pool = FakePool()
    pool.add_project_actor("demo", actor_user_id)
    pool.store["repository_authorization_flows"][authorization_id] = {
        "authorization_id": authorization_id,
        "project_id": "demo",
        "actor_user_id": actor_user_id,
        "state_hash": "a" * 64,
        "status": "awaiting_selection",
        "github_user_id": 77,
        "github_login": "octocat",
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        "completed_at": None,
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=11),
        "updated_at": datetime.now(timezone.utc),
    }
    pool.store["repository_authorization_candidates"][candidate_id] = {
        "candidate_id": candidate_id,
        "authorization_id": authorization_id,
        "installation_id": 42,
        "repository_id": 987,
        "repository_full_name": "private/widgets",
        "default_base_branch": "main",
        "private": True,
        "created_at": datetime.now(timezone.utc),
    }
    authorized_codegen_request("demo", actor_user_id=str(actor_user_id))
    app.state.pg_pool = pool

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/v1/github/repository-authorizations/{authorization_id}",
            params={"project_id": "demo"},
        )

    assert response.status_code == 410
    assert response.json()["detail"] == "Authorization expired"
    assert authorization_id not in pool.store["repository_authorization_flows"]
    assert candidate_id not in pool.store["repository_authorization_candidates"]


@pytest.mark.asyncio
async def test_two_ready_flows_replace_binding_without_two_active_grants():
    pool = FakePool()
    actor_user_id = uuid.uuid4()
    pool.add_project_actor("demo", actor_user_id)
    flow_candidates: list[tuple[uuid.UUID, uuid.UUID]] = []
    for index in (1, 2):
        authorization_id = uuid.uuid4()
        candidate_id = uuid.uuid4()
        flow_candidates.append((authorization_id, candidate_id))
        pool.store["repository_authorization_flows"][authorization_id] = {
            "authorization_id": authorization_id,
            "project_id": "demo",
            "actor_user_id": actor_user_id,
            "state_hash": f"{index}" * 64,
            "status": "awaiting_selection",
            "github_user_id": 77,
            "github_login": "octocat",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "completed_at": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        pool.store["repository_authorization_candidates"][candidate_id] = {
            "candidate_id": candidate_id,
            "authorization_id": authorization_id,
            "installation_id": 42,
            "repository_id": 900 + index,
            "repository_full_name": f"acme/repository-{index}",
            "default_base_branch": "main",
            "private": True,
            "created_at": datetime.now(timezone.utc),
        }

    for authorization_id, candidate_id in flow_candidates:
        verified_candidate = await authorization_store.get_completion_candidate(
            pool,
            authorization_id=authorization_id,
            project_id="demo",
            actor_user_id=actor_user_id,
            candidate_id=candidate_id,
        )
        await authorization_store.complete_authorization(
            pool,
            authorization_id=authorization_id,
            project_id="demo",
            actor_user_id=actor_user_id,
            candidate_id=candidate_id,
            verified_candidate=verified_candidate,
        )

    active = [
        row
        for row in pool.store["repository_grants"].values()
        if row["status"] == "active"
    ]
    assert len(active) == 1
    assert active[0]["repository_id"] == 902
    assert pool.store["connections"]["demo"]["grant_id"] == active[0]["grant_id"]
