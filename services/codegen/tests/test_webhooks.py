"""Tests for the GitHub webhook receiver (HMAC verify + branch routing)."""

import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import webhooks
from tests.fakes import FakePool


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _patch_sync(monkeypatch) -> list:
    synced: list = []

    async def fake_sync(pool, changeset_id, **kwargs):
        synced.append(changeset_id)

    monkeypatch.setattr(webhooks, "sync_ci_status", fake_sync)
    app.state.ci_deps = {"get_status": None, "mint_token": None, "mark_ready": None}
    return synced


@pytest.mark.asyncio
async def test_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    app.state.pg_pool = FakePool()
    _patch_sync(monkeypatch)
    async with _client() as client:
        resp = await client.post(
            "/webhooks/github",
            content=b"{}",
            headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "check_run"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_routes_check_run_to_ci_sync(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)  # permissive dev
    pool = FakePool()
    pool.add_changeset("cs_w1", "demo", status="pr_open", branch="apdl/x", pr_number=5)
    app.state.pg_pool = pool
    synced = _patch_sync(monkeypatch)

    body = json.dumps({"check_run": {"check_suite": {"head_branch": "apdl/x"}}}).encode()
    async with _client() as client:
        resp = await client.post(
            "/webhooks/github", content=body, headers={"X-GitHub-Event": "check_run"}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert synced == ["cs_w1"]


@pytest.mark.asyncio
async def test_accepts_valid_signature_and_routes_pull_request(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    pool = FakePool()
    pool.add_changeset("cs_w2", "demo", status="pr_open", branch="apdl/y", pr_number=5)
    app.state.pg_pool = pool
    synced = _patch_sync(monkeypatch)

    body = json.dumps({"pull_request": {"head": {"ref": "apdl/y"}}}).encode()
    async with _client() as client:
        resp = await client.post(
            "/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": _sign(body, "s3cret"), "X-GitHub-Event": "pull_request"},
        )

    assert resp.status_code == 200
    assert synced == ["cs_w2"]


@pytest.mark.asyncio
async def test_unknown_branch_is_ignored(monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    app.state.pg_pool = FakePool()
    synced = _patch_sync(monkeypatch)

    body = json.dumps({"check_run": {"check_suite": {"head_branch": "nope"}}}).encode()
    async with _client() as client:
        resp = await client.post(
            "/webhooks/github", content=body, headers={"X-GitHub-Event": "check_run"}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "no_changeset"
    assert synced == []
