"""Endpoint tests for the changeset lifecycle."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from tests.fakes import FakePool


def _client(pool: FakePool) -> AsyncClient:
    app.state.pg_pool = pool
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_changeset_requires_connection():
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/changesets",
            json={"project_id": "demo", "task": {"title": "x", "spec": "do the thing"}},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_get_and_list_changeset():
    pool = FakePool()
    pool.add_connection("demo")
    async with _client(pool) as client:
        created = await client.post(
            "/v1/changesets",
            json={
                "project_id": "demo",
                "run_id": "run-1",
                "task": {"title": "Add dark mode", "spec": "Implement a dark-mode toggle."},
            },
        )
        assert created.status_code == 202
        cs = created.json()
        assert cs["status"] == "queued"
        assert cs["base_branch"] == "main"
        assert cs["changeset_id"].startswith("cs_")

        cid = cs["changeset_id"]
        got = await client.get(f"/v1/changesets/{cid}")
        assert got.status_code == 200
        assert got.json()["changeset_id"] == cid

        listed = await client.get("/v1/changesets", params={"project_id": "demo"})
        assert listed.status_code == 200
        assert [c["changeset_id"] for c in listed.json()] == [cid]


@pytest.mark.asyncio
async def test_get_unknown_changeset_404():
    async with _client(FakePool()) as client:
        resp = await client.get("/v1/changesets/cs_nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_abandon_changeset_transitions_to_abandoned():
    pool = FakePool()
    pool.add_connection("demo")
    async with _client(pool) as client:
        created = await client.post(
            "/v1/changesets",
            json={"project_id": "demo", "task": {"title": "x", "spec": "do it"}},
        )
        cid = created.json()["changeset_id"]
        resp = await client.post(f"/v1/changesets/{cid}/abandon")
        assert resp.status_code == 200
        assert resp.json()["status"] == "abandoned"


@pytest.mark.asyncio
async def test_create_changeset_rejects_unknown_field():
    pool = FakePool()
    pool.add_connection("demo")
    async with _client(pool) as client:
        resp = await client.post(
            "/v1/changesets",
            json={"project_id": "demo", "task": {"title": "x", "spec": "y"}, "extra": 1},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_revert_merged_changeset_enqueues_a_revert():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_orig", "demo", status="merged", pr_number=7, branch="apdl/x")
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_orig/revert")
    assert resp.status_code == 202
    body = resp.json()
    assert body["changeset_id"].startswith("cs_")
    assert body["changeset_id"] != "cs_orig"
    assert body["status"] == "queued"
    assert body["task"]["title"].startswith("Revert:")
    assert "#7" in body["task"]["spec"]
    assert body["task"]["context"]["reverts_changeset"] == "cs_orig"


@pytest.mark.asyncio
async def test_revert_non_merged_changeset_409():
    pool = FakePool()
    pool.add_changeset("cs_open", "demo", status="pr_open", pr_number=7, branch="apdl/x")
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_open/revert")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_revert_unknown_changeset_404():
    async with _client(FakePool()) as client:
        resp = await client.post("/v1/changesets/cs_nope/revert")
    assert resp.status_code == 404
