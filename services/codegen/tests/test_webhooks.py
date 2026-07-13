"""GitHub webhooks are authenticated triggers for live GitHub observation."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import webhooks
from tests.fakes import FakePool

_TEST_WEBHOOK_SECRET = "s3cret"
_REPOSITORY_ID = 10
_INSTALLATION_ID = 1


@pytest.fixture(autouse=True)
def configured_webhook_secret(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _patch_sync(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_sync(pool, changeset_id, **kwargs):
        calls.append(
            {
                "pool": pool,
                "changeset_id": changeset_id,
                **kwargs,
            }
        )

    monkeypatch.setattr(webhooks, "sync_github_state", fake_sync)
    app.state.github_sync_deps = {
        "get_pull_request": object(),
        "get_ci_evidence": object(),
        "mint_token": object(),
        "repair_failure": object(),
    }
    return calls


def _seed_open(pool: FakePool, changeset_id: str = "cs-webhook") -> None:
    pool.add_connection(
        "demo",
        repo="acme/widgets",
        repository_id=_REPOSITORY_ID,
        installation_id=_INSTALLATION_ID,
    )
    pool.add_changeset(
        changeset_id,
        "demo",
        status="pr_open",
        branch="apdl/shared-name",
        pr_number=17,
        head_sha="head-exact",
        github_pr_status="open",
        external_ci_status="pending",
    )


async def _post(
    payload: dict,
    *,
    event: str,
    headers: dict | None = None,
    sign: bool = True,
):
    body = json.dumps(payload).encode()
    request_headers = {"X-GitHub-Event": event, **(headers or {})}
    if sign:
        request_headers.setdefault(
            "X-Hub-Signature-256", _sign(body, _TEST_WEBHOOK_SECRET)
        )
    async with _client() as client:
        return await client.post(
            "/webhooks/github",
            content=body,
            headers=request_headers,
        )


def test_every_webhook_route_requires_hmac_dependency():
    routes = list(webhooks.router.routes)
    secured_paths = {route.path for route in routes}
    app_webhook_paths = {
        path for path in app.openapi()["paths"] if path.startswith("/webhooks/")
    }

    assert routes
    assert app_webhook_paths == secured_paths
    for route in routes:
        dependency_calls = {
            dependency.call for dependency in route.dependant.dependencies
        }
        assert webhooks._verify_github_signature in dependency_calls


@pytest.mark.asyncio
async def test_rejects_unset_secret_before_routing(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    app.state.pg_pool = FakePool()
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
            "sha": "head-exact",
        },
        event="status",
        sign=False,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "GitHub webhook endpoint is disabled until a secret is configured."
    )
    assert calls == []


@pytest.mark.asyncio
async def test_rejects_invalid_signature_before_routing(monkeypatch):
    app.state.pg_pool = FakePool()
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
            "sha": "head-exact",
        },
        event="status",
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 401
    assert calls == []


@pytest.mark.asyncio
async def test_valid_signature_routes_exact_head_status_event(monkeypatch):
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)
    payload = {
        "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
        "installation": {"id": _INSTALLATION_ID},
        "sha": "head-exact",
    }
    body = json.dumps(payload).encode()

    async with _client() as client:
        response = await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-GitHub-Event": "status",
                "X-Hub-Signature-256": _sign(body, _TEST_WEBHOOK_SECRET),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "queued",
        "changeset_id": "cs-webhook",
    }
    assert [call["changeset_id"] for call in calls] == ["cs-webhook"]
    assert calls[0]["pr_action"] == "polled"
    assert calls[0]["delivery_id"] is None


@pytest.mark.asyncio
async def test_check_run_routes_by_exact_head_sha_not_branch(monkeypatch):
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "check_run": {
                "head_sha": "head-exact",
                "check_suite": {
                    "head_branch": "a-misleading-branch",
                    "head_sha": "head-exact",
                },
            },
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
        },
        event="check_run",
    )

    assert response.json()["status"] == "queued"
    assert [call["changeset_id"] for call in calls] == ["cs-webhook"]


@pytest.mark.asyncio
async def test_same_head_in_another_repository_does_not_route(monkeypatch):
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "check_suite": {"head_sha": "head-exact"},
            # A slug is display metadata; the immutable numeric id controls
            # routing even when an event supplies the expected name.
            "repository": {"id": 11, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
        },
        event="check_suite",
    )

    assert response.json() == {"status": "no_changeset"}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "opened",
        "ready_for_review",
        "converted_to_draft",
        "synchronize",
        "closed",
        "reopened",
    ],
)
async def test_pull_request_actions_queue_live_observation_by_repo_and_number(
    monkeypatch, action
):
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "action": action,
            "number": 17,
            "pull_request": {
                "number": 17,
                # Routing deliberately ignores a branch name supplied by the
                # event; live GitHub state is fetched by immutable PR identity.
                "head": {"ref": "not-the-stored-branch"},
            },
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
        },
        event="pull_request",
        headers={"X-GitHub-Delivery": f"delivery-{action}"},
    )

    assert response.json()["status"] == "queued"
    assert len(calls) == 1
    assert calls[0]["changeset_id"] == "cs-webhook"
    assert calls[0]["pr_action"] == action
    assert calls[0]["delivery_id"] == f"delivery-{action}"


@pytest.mark.asyncio
async def test_pull_request_event_requires_delivery_identity(monkeypatch):
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "action": "synchronize",
            "number": 17,
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
        },
        event="pull_request",
    )

    assert response.status_code == 400
    assert "delivery ID" in response.json()["detail"]
    assert calls == []


@pytest.mark.asyncio
async def test_closed_payload_does_not_directly_merge_or_abandon_changeset(monkeypatch):
    """The event is only a trigger; the subsequent live GitHub read is authoritative."""
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "action": "closed",
            "number": 17,
            "pull_request": {
                "number": 17,
                "merged": True,
                "merge_commit_sha": "untrusted-payload-sha",
            },
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
        },
        event="pull_request",
        headers={"X-GitHub-Delivery": "delivery-close"},
    )

    assert response.json()["status"] == "queued"
    row = pool.store["changesets"]["cs-webhook"]
    assert row["status"] == "pr_open"
    assert row["merge_sha"] is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_or_incomplete_event_is_ignored(monkeypatch):
    app.state.pg_pool = FakePool()
    calls = _patch_sync(monkeypatch)

    response = await _post(
        {
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
        },
        event="check_run",
    )

    assert response.json() == {"status": "ignored"}
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "repository": {"full_name": "acme/widgets"},
            "installation": {"id": _INSTALLATION_ID},
            "sha": "head-exact",
        },
        {
            "repository": {"id": _REPOSITORY_ID, "full_name": "acme/widgets"},
            "sha": "head-exact",
        },
    ],
)
async def test_event_missing_immutable_repository_identity_is_ignored(
    monkeypatch, payload
):
    pool = FakePool()
    _seed_open(pool)
    app.state.pg_pool = pool
    calls = _patch_sync(monkeypatch)

    response = await _post(payload, event="status")

    assert response.json() == {"status": "ignored"}
    assert calls == []
