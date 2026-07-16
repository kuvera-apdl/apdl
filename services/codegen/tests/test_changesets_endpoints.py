"""Endpoint tests for the changeset lifecycle."""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.evaluations.models import RolloutStage
from app.models.observations import CIVerificationObservation, ExternalCIStatus
from app.runtime.collector import RuntimeEvidenceCollection
from app.runtime.evidence import build_runtime_evidence_observation
from app.runtime.models import RuntimeAcceptancePlan
from app.store.runtime_evidence import apply_runtime_evidence_observation
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
async def test_evaluation_only_stage_rejects_changeset_before_queueing():
    pool = FakePool()
    pool.add_connection("demo")
    app.state.codegen_rollout_stage = RolloutStage.shadow
    try:
        async with _client(pool) as client:
            response = await client.post(
                "/v1/changesets",
                json={
                    "project_id": "demo",
                    "task": {"title": "x", "spec": "do the thing"},
                },
            )
    finally:
        del app.state.codegen_rollout_stage

    assert response.status_code == 409
    assert "publication are disabled" in response.json()["detail"]
    assert pool.store["changesets"] == {}


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
async def test_changeset_routes_reject_another_project(
    authorized_codegen_request,
):
    pool = FakePool()
    pool.add_connection("other")
    pool.add_changeset("cs-other", "other", status="merged", pr_number=7)
    authorized_codegen_request("demo", frozenset({"agents:read", "agents:manage"}))

    async with _client(pool) as client:
        responses = [
            await client.get("/v1/changesets", params={"project_id": "other"}),
            await client.get("/v1/changesets/cs-other"),
            await client.get("/v1/changesets/cs-other/observations"),
            await client.get("/v1/changesets/cs-other/runtime-observations"),
            await client.post("/v1/changesets/cs-other/abandon"),
            await client.post("/v1/changesets/cs-other/revert"),
            await client.post("/v1/changesets/cs-other/retry"),
        ]
        create = await client.post(
            "/v1/changesets",
            json={
                "project_id": "other",
                "task": {"title": "x", "spec": "do the thing"},
            },
        )

    assert all(response.status_code == 403 for response in [*responses, create])


@pytest.mark.asyncio
async def test_changeset_mutation_requires_manage_role(authorized_codegen_request):
    pool = FakePool()
    pool.add_connection("demo")
    authorized_codegen_request("demo", frozenset({"agents:read"}))
    async with _client(pool) as client:
        response = await client.post(
            "/v1/changesets",
            json={
                "project_id": "demo",
                "task": {"title": "x", "spec": "do the thing"},
            },
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_runtime_observations_endpoint_returns_exact_head_journal():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets")
    pool.add_changeset(
        "cs-runtime",
        "demo",
        status="pr_open",
        pr_number=7,
        branch="apdl/x",
        head_sha="head-a",
        github_pr_status="open",
        external_ci_status="unverified_external_ci",
    )
    plan = RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
        repo="acme/widgets",
        branch="apdl/x",
    )
    observation = build_runtime_evidence_observation(
        changeset_id="cs-runtime",
        repository="acme/widgets",
        pr_number=7,
        head_sha="head-a",
        ci_observation=CIVerificationObservation(
            observation_id="ciobs_" + "d" * 32,
            changeset_id="cs-runtime",
            repository="acme/widgets",
            pr_number=7,
            head_sha="head-a",
            status=ExternalCIStatus.unverified_external_ci,
            observed_at=datetime(2026, 7, 11, tzinfo=UTC),
        ),
        plan=plan,
        collection=RuntimeEvidenceCollection(head_sha="head-a"),
        observed_at=datetime(2026, 7, 11, tzinfo=UTC),
    )
    await apply_runtime_evidence_observation(pool, observation)

    async with _client(pool) as client:
        response = await client.get(
            "/v1/changesets/cs-runtime/runtime-observations"
        )

    assert response.status_code == 200
    assert [item["observation_id"] for item in response.json()] == [
        observation.observation_id
    ]


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
    pool.add_changeset(
        "cs_orig", "demo", status="merged", pr_number=7, branch="apdl/x",
        merge_sha="deadbeef123",
    )
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
    # The recorded merge SHA rides along so the editor reverts deterministically.
    assert body["task"]["context"]["revert_sha"] == "deadbeef123"
    assert "deadbeef123" in body["task"]["spec"]


@pytest.mark.asyncio
async def test_revert_without_recorded_sha_falls_back_to_prose():
    # A changeset merged before merge_sha existed still gets a revert task —
    # just without the deterministic target.
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_old", "demo", status="merged", pr_number=3, branch="apdl/y")
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_old/revert")
    assert resp.status_code == 202
    assert "revert_sha" not in resp.json()["task"]["context"]


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


@pytest.mark.asyncio
async def test_retry_pre_pr_error_enqueues_same_task():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_bad", "demo", status="error", base_branch="develop")
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_bad/retry")
    assert resp.status_code == 202
    body = resp.json()
    assert body["changeset_id"].startswith("cs_")
    assert body["changeset_id"] != "cs_bad"
    assert body["status"] == "queued"
    # Same task, same base branch — re-run verbatim, with a lineage marker.
    assert body["task"]["title"] == "t"
    assert body["task"]["spec"] == "spec spec spec"
    assert body["base_branch"] == "develop"
    assert body["task"]["context"]["retry_of"] == "cs_bad"


@pytest.mark.parametrize(
    "status", ["merged", "queued", "editing", "pushing", "pr_open", "abandoned"]
)
@pytest.mark.asyncio
async def test_retry_non_failed_changeset_409(status):
    pool = FakePool()
    pool.add_changeset("cs_x", "demo", status=status)
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_x/retry")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_retry_unknown_changeset_404():
    async with _client(FakePool()) as client:
        resp = await client.post("/v1/changesets/cs_nope/retry")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_abandon_open_pr_is_rejected_and_github_remains_authoritative():
    pool = FakePool()
    pool.add_connection("demo", repo="acme/widgets", installation_id=42)
    pool.add_changeset("cs_open", "demo", status="pr_open", pr_number=7, branch="apdl/x")
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_open/abandon")
    assert resp.status_code == 409
    assert "managed on GitHub" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_retry_closed_pr_cannot_create_replacement_pr():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_closed", "demo", status="abandoned", pr_number=7, branch="apdl/x"
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_closed/retry")
    assert resp.status_code == 409
