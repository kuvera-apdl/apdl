"""Endpoint tests for the changeset lifecycle."""

import asyncio
import json
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from app import capabilities
from app.main import app
from app.evaluations.models import RolloutStage
from app.models.observations import CIVerificationObservation, ExternalCIStatus
from app.runtime.collector import RuntimeEvidenceCollection
from app.runtime.evidence import build_runtime_evidence_observation
from app.runtime.models import RuntimeAcceptancePlan
from app.routers import changesets as changesets_router
from app.safety.policy import TenantCodegenConnectionPolicy
from app.store.runtime_evidence import apply_runtime_evidence_observation
from tests.fakes import FakePool


@pytest.fixture(autouse=True)
def executable_changeset_runtime(monkeypatch):
    """Give lifecycle tests an executable runtime without external Docker/GitHub."""
    app.state.codegen_rollout_stage = RolloutStage.development_pr
    app.state.job_deps = {
        "editor": object(),
        "mint_read_token": object(),
        "mint_write_token": object(),
        "mint_pr_write_token": object(),
        "branch_publisher": object(),
        "open_pr": object(),
        "find_pr": object(),
        "close_pr": object(),
        "publication_gate": object(),
    }
    monkeypatch.setattr(capabilities, "_github_app_configured", lambda: True)
    monkeypatch.setattr(capabilities, "_provider_configured", lambda: True)
    monkeypatch.setattr(capabilities, "_assert_runtime_ready", lambda *_: None)
    monkeypatch.setattr(changesets_router, "_maybe_enqueue", lambda *_: None)
    monkeypatch.delenv("CODEGEN_KILL_SWITCH", raising=False)
    monkeypatch.delenv("CODEGEN_DISABLED_PROJECTS", raising=False)
    yield
    for name in ("codegen_rollout_stage", "job_deps"):
        if hasattr(app.state, name):
            delattr(app.state, name)


def _client(pool: FakePool) -> AsyncClient:
    app.state.pg_pool = pool
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_changeset_requires_connection():
    async with _client(FakePool()) as client:
        resp = await client.post(
            "/v1/changesets",
            json={
                "project_id": "demo",
                "idempotency_key": "test:requires-connection",
                "task": {"title": "x", "spec": "do the thing"},
            },
        )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "changeset_creation_disabled",
        "reasons": ["repository_grant_missing"],
    }


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
                    "idempotency_key": "test:evaluation-stage",
                    "task": {"title": "x", "spec": "do the thing"},
                },
            )
    finally:
        app.state.codegen_rollout_stage = RolloutStage.development_pr

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "changeset_creation_disabled",
        "reasons": ["rollout_stage_blocked", "runtime_unavailable"],
    }
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
                "idempotency_key": "test:create-dark-mode",
                "run_id": "run-1",
                "task": {"title": "Add dark mode", "spec": "Implement a dark-mode toggle."},
            },
        )
        assert created.status_code == 202
        cs = created.json()
        assert cs["status"] == "queued"
        assert cs["base_branch"] == "main"
        assert cs["changeset_id"].startswith("cs_")
        assert "control_metadata" not in cs
        controls = json.loads(
            pool.store["changesets"][cs["changeset_id"]]["control_metadata"]
        )
        assert controls == {
            "schema_version": "changeset_controls@1",
            "risk_level": "high",
            "revert": None,
        }

        cid = cs["changeset_id"]
        got = await client.get(f"/v1/changesets/{cid}")
        assert got.status_code == 200
        assert got.json()["changeset_id"] == cid

        listed = await client.get("/v1/changesets", params={"project_id": "demo"})
        assert listed.status_code == 200
        assert [c["changeset_id"] for c in listed.json()] == [cid]


@pytest.mark.asyncio
async def test_create_changeset_is_idempotent_under_concurrent_retries(monkeypatch):
    pool = FakePool()
    pool.add_connection("demo")
    enqueued: list[str] = []
    monkeypatch.setattr(
        changesets_router,
        "_maybe_enqueue",
        lambda app, background_tasks, changeset_id: enqueued.append(changeset_id),
    )
    body = {
        "project_id": "demo",
        "idempotency_key": "agent-effect:command-1:changeset-1",
        "run_id": "run-1",
        "base_branch": "main",
        "task": {"title": "Add dark mode", "spec": "Add the toggle."},
    }

    async with _client(pool) as client:
        first, second = await asyncio.gather(
            client.post("/v1/changesets", json=body),
            client.post("/v1/changesets", json=body),
        )
        third = await client.post("/v1/changesets", json=body)

    assert [first.status_code, second.status_code, third.status_code] == [202, 202, 202]
    changeset_ids = {
        first.json()["changeset_id"],
        second.json()["changeset_id"],
        third.json()["changeset_id"],
    }
    assert len(changeset_ids) == 1
    assert len(pool.store["changesets"]) == 1
    assert enqueued == [next(iter(changeset_ids))]


@pytest.mark.parametrize(
    "changed_field",
    [
        "run_id",
        "base_branch",
        "task",
    ],
)
@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_canonical_request(changed_field):
    pool = FakePool()
    pool.add_connection("demo")
    body = {
        "project_id": "demo",
        "idempotency_key": f"test:immutable:{changed_field}",
        "run_id": "run-1",
        "base_branch": "main",
        "task": {"title": "Add dark mode", "spec": "Add the toggle."},
    }
    second_body = {**body, "task": dict(body["task"])}

    async with _client(pool) as client:
        first = await client.post("/v1/changesets", json=body)
        assert first.status_code == 202

        if changed_field == "run_id":
            second_body["run_id"] = "run-2"
        elif changed_field == "base_branch":
            second_body["base_branch"] = "develop"
        elif changed_field == "task":
            second_body["task"]["spec"] = "A different implementation."
        second = await client.post("/v1/changesets", json=second_body)

    assert second.status_code == 409
    assert "canonical request payload" in second.json()["detail"]
    assert len(pool.store["changesets"]) == 1


@pytest.mark.parametrize("change", ["repository_target", "policy_snapshot", "removed"])
@pytest.mark.asyncio
async def test_exact_replay_returns_original_after_mutable_connection_change(change):
    pool = FakePool()
    pool.add_connection("demo")
    body = {
        "project_id": "demo",
        "idempotency_key": f"test:replay:{change}",
        "run_id": "run-1",
        "task": {"title": "Add dark mode", "spec": "Add the toggle."},
    }

    async with _client(pool) as client:
        first = await client.post("/v1/changesets", json=body)
        assert first.status_code == 202

        if change == "repository_target":
            pool.add_connection(
                "demo",
                repo="acme/other",
                installation_id=2,
                repository_id=20,
                grant_id="ghg_demoother",
            )
        elif change == "policy_snapshot":
            pool.add_connection(
                "demo",
                tenant_policy=TenantCodegenConnectionPolicy(test_cmd="make verify"),
            )
        else:
            del pool.store["connections"]["demo"]

        replay = await client.post("/v1/changesets", json=body)

    assert replay.status_code == 202
    assert replay.json()["changeset_id"] == first.json()["changeset_id"]
    assert len(pool.store["changesets"]) == 1


@pytest.mark.asyncio
async def test_request_digest_distinguishes_json_boolean_from_number():
    pool = FakePool()
    pool.add_connection("demo")
    first_body = {
        "project_id": "demo",
        "idempotency_key": "test:typed-context",
        "task": {
            "title": "Typed context",
            "spec": "Preserve JSON types.",
            "context": {"value": True},
        },
    }
    second_body = {
        **first_body,
        "task": {**first_body["task"], "context": {"value": 1}},
    }

    async with _client(pool) as client:
        first = await client.post("/v1/changesets", json=first_body)
        second = await client.post("/v1/changesets", json=second_body)

    assert first.status_code == 202
    assert second.status_code == 409
    assert "canonical request payload" in second.json()["detail"]


@pytest.mark.parametrize(
    "idempotency_key",
    [None, "", "contains whitespace", "-starts-with-punctuation", "x" * 201],
)
@pytest.mark.asyncio
async def test_create_changeset_requires_canonical_idempotency_key(idempotency_key):
    pool = FakePool()
    pool.add_connection("demo")
    body = {
        "project_id": "demo",
        "task": {"title": "x", "spec": "do it"},
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key

    async with _client(pool) as client:
        response = await client.post("/v1/changesets", json=body)

    assert response.status_code == 422
    assert pool.store["changesets"] == {}


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
                "idempotency_key": "test:other-project",
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
                "idempotency_key": "test:manage-role",
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
            json={
                "project_id": "demo",
                "idempotency_key": "test:abandon",
                "task": {"title": "x", "spec": "do it"},
            },
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
            json={
                "project_id": "demo",
                "idempotency_key": "test:unknown-field",
                "task": {"title": "x", "spec": "y"},
                "extra": 1,
            },
        )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "reserved_key",
    [
        "risk_level",
        "revert_sha",
        "reverts_changeset",
        "reverts_pr_number",
        "retry_of",
    ],
)
@pytest.mark.asyncio
async def test_create_changeset_rejects_private_control_keys_in_context(
    reserved_key,
):
    pool = FakePool()
    pool.add_connection("demo")
    async with _client(pool) as client:
        response = await client.post(
            "/v1/changesets",
            json={
                "project_id": "demo",
                "idempotency_key": f"test:reserved:{reserved_key}",
                "task": {
                    "title": "x",
                    "spec": "do it",
                    "context": {reserved_key: "attacker-controlled"},
                },
            },
        )

    assert response.status_code == 422
    assert "reserved private control keys" in response.text
    assert pool.store["changesets"] == {}


@pytest.mark.asyncio
async def test_revert_merged_changeset_enqueues_one_idempotent_revert(monkeypatch):
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_orig", "demo", status="merged", pr_number=7, branch="apdl/x",
        merge_sha="deadbeef123",
    )
    enqueued: list[str] = []
    monkeypatch.setattr(
        changesets_router,
        "_maybe_enqueue",
        lambda app, background_tasks, changeset_id: enqueued.append(changeset_id),
    )
    async with _client(pool) as client:
        resp = await client.post("/v1/changesets/cs_orig/revert")
        duplicate = await client.post("/v1/changesets/cs_orig/revert")
    assert resp.status_code == 202
    assert duplicate.status_code == 202
    body = resp.json()
    assert duplicate.json()["changeset_id"] == body["changeset_id"]
    assert body["changeset_id"].startswith("cs_")
    assert body["changeset_id"] != "cs_orig"
    assert body["status"] == "queued"
    assert body["task"]["title"].startswith("Revert:")
    assert "#7" in body["task"]["spec"]
    assert body["task"]["context"] == {}
    assert "deadbeef123" in body["task"]["spec"]
    assert "control_metadata" not in body
    controls = json.loads(
        pool.store["changesets"][body["changeset_id"]]["control_metadata"]
    )
    assert controls == {
        "schema_version": "changeset_controls@1",
        "risk_level": "high",
        "revert": {
            "source_changeset_id": "cs_orig",
            "merge_sha": "deadbeef123",
        },
    }
    assert enqueued == [body["changeset_id"]]


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
    body = resp.json()
    assert body["task"]["context"] == {}
    controls = json.loads(
        pool.store["changesets"][body["changeset_id"]]["control_metadata"]
    )
    assert controls["revert"] == {
        "source_changeset_id": "cs_old",
        "merge_sha": None,
    }


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
    # Same public task and base branch; lineage remains private and relational.
    assert body["task"]["title"] == "t"
    assert body["task"]["spec"] == "spec spec spec"
    assert body["base_branch"] == "develop"
    assert body["task"]["context"] == {}
    assert pool.store["changesets"][body["changeset_id"]]["retry_of_changeset_id"] == "cs_bad"
    assert json.loads(
        pool.store["changesets"][body["changeset_id"]]["control_metadata"]
    ) == {
        "schema_version": "changeset_controls@1",
        "risk_level": "high",
        "revert": None,
    }


@pytest.mark.asyncio
async def test_retry_preserves_authorized_revert_controls_privately():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset(
        "cs_source",
        "demo",
        status="merged",
        pr_number=8,
        branch="apdl/source",
        merge_sha="merge-sha-8",
    )

    async with _client(pool) as client:
        revert_response = await client.post("/v1/changesets/cs_source/revert")
        revert_id = revert_response.json()["changeset_id"]
        pool.store["changesets"][revert_id]["status"] = "error"
        retry_response = await client.post(f"/v1/changesets/{revert_id}/retry")

    assert revert_response.status_code == 202
    assert retry_response.status_code == 202
    retry = retry_response.json()
    assert retry["task"]["context"] == {}
    assert "control_metadata" not in retry
    retry_row = pool.store["changesets"][retry["changeset_id"]]
    assert retry_row["retry_of_changeset_id"] == revert_id
    assert json.loads(retry_row["control_metadata"]) == {
        "schema_version": "changeset_controls@1",
        "risk_level": "high",
        "revert": {
            "source_changeset_id": "cs_source",
            "merge_sha": "merge-sha-8",
        },
    }


@pytest.mark.asyncio
async def test_retry_is_idempotent_for_duplicate_and_concurrent_requests(monkeypatch):
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_bad", "demo", status="error")
    enqueued: list[str] = []
    monkeypatch.setattr(
        changesets_router,
        "_maybe_enqueue",
        lambda app, background_tasks, changeset_id: enqueued.append(changeset_id),
    )

    async with _client(pool) as client:
        first, second = await asyncio.gather(
            client.post("/v1/changesets/cs_bad/retry"),
            client.post("/v1/changesets/cs_bad/retry"),
        )
        third = await client.post("/v1/changesets/cs_bad/retry")

    assert [first.status_code, second.status_code, third.status_code] == [202, 202, 202]
    child_ids = {
        first.json()["changeset_id"],
        second.json()["changeset_id"],
        third.json()["changeset_id"],
    }
    assert len(child_ids) == 1
    children = [
        row
        for row in pool.store["changesets"].values()
        if row.get("retry_of_changeset_id") == "cs_bad"
    ]
    assert len(children) == 1
    assert enqueued == [next(iter(child_ids))]


@pytest.mark.asyncio
async def test_retry_never_returns_a_cross_project_legacy_child():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_bad", "demo", status="error")
    pool.add_changeset("cs_foreign_child", "other", status="queued")
    pool.store["changesets"]["cs_foreign_child"]["retry_of_changeset_id"] = "cs_bad"

    async with _client(pool) as client:
        response = await client.post("/v1/changesets/cs_bad/retry")

    assert response.status_code == 409
    assert "another project" in response.json()["detail"]


@pytest.mark.asyncio
async def test_retry_rejects_server_key_bound_to_different_lineage():
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs_bad", "demo", status="error")
    pool.add_changeset("cs_other_parent", "demo", status="error")
    pool.add_changeset("cs_existing_child", "demo", status="queued")
    row = pool.store["changesets"]["cs_existing_child"]
    row["idempotency_key"] = changesets_router._derived_idempotency_key(
        "retry", "cs_bad"
    )
    row["retry_of_changeset_id"] = "cs_other_parent"

    async with _client(pool) as client:
        response = await client.post("/v1/changesets/cs_bad/retry")

    assert response.status_code == 409
    assert "different retry lineage" in response.json()["detail"]


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
