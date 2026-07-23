"""Tenant-scoped executable Codegen capability contracts."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app import capabilities
from app.auth import Principal, authenticate_request
from app.evaluations.models import RolloutStage
from app.main import app
from tests.fakes import FakePool


def _runtime_dependencies() -> dict[str, object]:
    return {
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


@pytest.fixture
def executable_runtime(monkeypatch):
    app.state.codegen_rollout_stage = RolloutStage.development_pr
    app.state.job_deps = _runtime_dependencies()
    monkeypatch.setattr(capabilities, "_github_app_configured", lambda: True)
    monkeypatch.setattr(capabilities, "_provider_configured", lambda: True)
    monkeypatch.setattr(capabilities, "_assert_runtime_ready", lambda *_: None)
    monkeypatch.delenv("CODEGEN_KILL_SWITCH", raising=False)
    monkeypatch.delenv("CODEGEN_DISABLED_PROJECTS", raising=False)
    yield
    for name in ("codegen_rollout_stage", "job_deps"):
        if hasattr(app.state, name):
            delattr(app.state, name)


@pytest.mark.asyncio
async def test_capability_is_authenticated_tenant_scoped_and_executable(
    executable_runtime,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    app.state.pg_pool = pool

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/capabilities/changeset-creation",
            params={"project_id": "demo"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "project_id": "demo",
        "changeset_creation": "available",
        "reasons": [],
        "checks": {
            "rollout_stage": "ready",
            "automation": "ready",
            "repository_grant": "ready",
            "github_app": "ready",
            "provider": "ready",
            "worker": "ready",
            "runtime": "ready",
        },
    }


@pytest.mark.asyncio
async def test_capability_reports_every_blocking_prerequisite(
    monkeypatch,
) -> None:
    pool = FakePool()
    app.state.pg_pool = pool
    app.state.codegen_rollout_stage = RolloutStage.shadow
    if hasattr(app.state, "job_deps"):
        del app.state.job_deps
    monkeypatch.setenv("CODEGEN_KILL_SWITCH", "true")
    monkeypatch.setattr(capabilities, "_github_app_configured", lambda: False)
    monkeypatch.setattr(capabilities, "_provider_configured", lambda: False)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/capabilities/changeset-creation",
            params={"project_id": "demo"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["changeset_creation"] == "disabled"
    assert body["reasons"] == [
        "rollout_stage_blocked",
        "automation_disabled",
        "repository_grant_missing",
        "github_app_unconfigured",
        "provider_unconfigured",
        "worker_unavailable",
        "runtime_unavailable",
    ]


@pytest.mark.asyncio
async def test_runtime_is_revalidated_for_each_capability_check(
    executable_runtime,
    monkeypatch,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    app.state.pg_pool = pool
    checks = 0

    def fail_runtime(*_args: object) -> None:
        nonlocal checks
        checks += 1
        raise RuntimeError("Docker daemon disappeared")

    monkeypatch.setattr(capabilities, "_assert_runtime_ready", fail_runtime)

    first = await capabilities.evaluate_changeset_creation(app, pool, "demo")
    second = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    assert checks == 2
    assert first.report.reasons == ["runtime_unavailable"]
    assert second.report.changeset_creation == "disabled"


@pytest.mark.asyncio
async def test_capability_rejects_cross_project_credentials(
    executable_runtime,
    authorized_codegen_request: Callable,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    app.state.pg_pool = pool
    authorized_codegen_request("other")

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/capabilities/changeset-creation",
            params={"project_id": "demo"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_capability_rejects_project_without_operator_execution_authority(
    executable_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del executable_runtime

    async def unauthorized(request: Request) -> Principal:
        principal = Principal(
            credential_id="test-credential",
            project_id="demo",
            roles=frozenset({"agents:manage"}),
            execution_authorized=False,
        )
        request.state.principal = principal
        return principal

    monkeypatch.setitem(app.dependency_overrides, authenticate_request, unauthorized)
    app.state.pg_pool = FakePool()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/v1/capabilities/changeset-creation",
            params={"project_id": "demo"},
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Codegen execution requires operator project authorization"
    }


@pytest.mark.parametrize(
    ("model", "environment"),
    [
        ("claude-opus-4-8", {"ANTHROPIC_API_KEY": "secret"}),
        ("openai/gpt-5", {"OPENAI_API_KEY": "secret"}),
        ("gemini/gemini-2.5-pro", {"GEMINI_API_KEY": "secret"}),
    ],
)
def test_provider_check_is_bound_to_the_selected_model(
    model: str,
    environment: dict[str, str],
    monkeypatch,
) -> None:
    for name in capabilities.MODEL_PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEGEN_MODEL", model)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert capabilities._provider_configured() is True

    for name in environment:
        monkeypatch.delenv(name)
    assert capabilities._provider_configured() is False


def test_vertex_project_metadata_is_not_an_executable_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in capabilities.MODEL_PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEGEN_MODEL", "vertex_ai/gemini-2.5-pro")
    monkeypatch.setenv("VERTEXAI_PROJECT", "project")
    monkeypatch.setenv("VERTEXAI_LOCATION", "region")

    assert capabilities._provider_configured() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_dependency",
    ["find_pr", "close_pr", "publication_gate"],
)
async def test_incomplete_job_contract_is_not_worker_ready(
    executable_runtime,
    missing_dependency: str,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    del app.state.job_deps[missing_dependency]

    evaluation = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    assert evaluation.report.changeset_creation == "disabled"
    assert evaluation.report.reasons == ["worker_unavailable", "runtime_unavailable"]
