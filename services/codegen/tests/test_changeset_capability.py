"""Tenant-scoped executable Codegen capability contracts."""

from __future__ import annotations

import asyncio
import base64
import threading
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app import capabilities
from app.auth import Principal, authenticate_request
from app.main import app
from app.models.execution import PublicationStage
from app.store.llm_credentials import ENCRYPTION_KEY_ENV
from tests.fakes import FakePool, TEST_LLM_CREDENTIAL_ENCRYPTION_KEY_ID


def _rsa_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


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


def test_github_app_capability_uses_decoded_base64_key(monkeypatch) -> None:
    private_key = _rsa_private_pem()
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY_BASE64",
        base64.b64encode(private_key.encode()).decode(),
    )

    assert capabilities._github_app_configured() is True


def test_github_app_capability_rejects_invalid_base64_without_leaking_it(
    monkeypatch,
    caplog,
) -> None:
    invalid_key = "not%%%base64%%%secret-sentinel"
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", invalid_key)

    assert capabilities._github_app_configured() is False
    assert "GITHUB_APP_PRIVATE_KEY_BASE64" in caplog.text
    assert invalid_key not in caplog.text


@pytest.fixture
def executable_runtime(monkeypatch):
    app.state.codegen_rollout_stage = PublicationStage.development_pr
    app.state.job_deps = _runtime_dependencies()
    monkeypatch.setattr(capabilities, "_github_app_configured", lambda: True)
    monkeypatch.setattr(capabilities, "_provider_configured", lambda: True)
    monkeypatch.setattr(
        capabilities,
        "_provider_encryption_key_id",
        lambda: TEST_LLM_CREDENTIAL_ENCRYPTION_KEY_ID,
    )
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
        "llm_assignments": [
            {
                "role": "editor",
                "provider": "anthropic",
                "model_id": "claude-sonnet-5",
                "connection_state": "active",
            },
            {
                "role": "helper",
                "provider": "anthropic",
                "model_id": "claude-sonnet-5",
                "connection_state": "active",
            },
        ],
    }


@pytest.mark.asyncio
async def test_capability_reports_every_blocking_prerequisite(
    monkeypatch,
) -> None:
    pool = FakePool()
    app.state.pg_pool = pool
    app.state.codegen_rollout_stage = PublicationStage.offline
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
    assert body["llm_assignments"] == []


@pytest.mark.asyncio
async def test_tenant_capability_uses_the_projects_exact_model_assignments(
    executable_runtime,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_llm_connection(
        "demo",
        helper_model_id="claude-haiku-4-5-20251001",
    )
    app.state.pg_pool = pool
    app.state.codegen_rollout_stage = PublicationStage.tenant_draft_pr

    capability = await capabilities.evaluate_changeset_creation(
        app,
        pool,
        "demo",
    )

    assert [item.model_id for item in capability.report.llm_assignments] == [
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    ]
    assert capability.report.changeset_creation == "available"
    assert capability.report.checks.provider == "ready"
    assert capability.report.reasons == []


@pytest.mark.asyncio
async def test_runtime_probe_failure_is_reused_within_ttl(
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
    monkeypatch.setattr(capabilities, "monotonic", lambda: 100.0)

    first = await capabilities.evaluate_changeset_creation(app, pool, "demo")
    second = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    assert checks == 1
    assert first.report.reasons == ["runtime_unavailable"]
    assert second.report.changeset_creation == "disabled"


@pytest.mark.asyncio
async def test_concurrent_capability_checks_share_one_runtime_probe(
    executable_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    app.state.pg_pool = pool
    started = threading.Event()
    release = threading.Event()
    calls: list[object] = []

    def blocking_runtime(editor: object, *_args: object) -> None:
        calls.append(editor)
        started.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test did not release runtime probe")

    monkeypatch.setattr(capabilities, "_assert_runtime_ready", blocking_runtime)
    monkeypatch.setattr(capabilities, "monotonic", lambda: 100.0)

    tasks = [
        asyncio.create_task(
            capabilities.evaluate_changeset_creation(app, pool, "demo")
        )
        for _ in range(4)
    ]
    started_in_time = await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0)
    release.set()
    evaluations = await asyncio.gather(*tasks)

    assert started_in_time is True
    assert len(calls) == 1
    assert all(
        evaluation.report.changeset_creation == "available"
        for evaluation in evaluations
    )


@pytest.mark.asyncio
async def test_runtime_probe_is_repeated_after_ttl_expiry(
    executable_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    clock = [100.0]
    checks = 0

    def ready_runtime(*_args: object) -> None:
        nonlocal checks
        checks += 1

    monkeypatch.setattr(capabilities, "_assert_runtime_ready", ready_runtime)
    monkeypatch.setattr(capabilities, "monotonic", lambda: clock[0])

    await capabilities.evaluate_changeset_creation(app, pool, "demo")
    await capabilities.evaluate_changeset_creation(app, pool, "demo")
    assert checks == 1

    clock[0] += capabilities._RUNTIME_PROBE_TTL_SECONDS
    evaluation = await capabilities.evaluate_changeset_creation(
        app,
        pool,
        "demo",
    )

    assert checks == 2
    assert evaluation.report.changeset_creation == "available"


@pytest.mark.asyncio
async def test_runtime_probe_cache_key_binds_stage_editor_and_revision(
    executable_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    original_editor = app.state.job_deps["editor"]
    revision = ["revision-a"]
    calls: list[tuple[object, PublicationStage, str]] = []

    def ready_runtime(
        editor: object,
        stage: PublicationStage,
        expected_revision: str,
    ) -> None:
        calls.append((editor, stage, expected_revision))

    monkeypatch.setattr(capabilities, "_assert_runtime_ready", ready_runtime)
    monkeypatch.setattr(capabilities, "codegen_revision", lambda: revision[0])
    monkeypatch.setattr(capabilities, "monotonic", lambda: 100.0)

    await capabilities.evaluate_changeset_creation(app, pool, "demo")
    await capabilities.evaluate_changeset_creation(app, pool, "demo")

    app.state.codegen_rollout_stage = PublicationStage.tenant_draft_pr
    await capabilities.evaluate_changeset_creation(app, pool, "demo")

    replacement_editor = object()
    app.state.job_deps["editor"] = replacement_editor
    await capabilities.evaluate_changeset_creation(app, pool, "demo")

    revision[0] = "revision-b"
    await capabilities.evaluate_changeset_creation(app, pool, "demo")

    assert calls == [
        (original_editor, PublicationStage.development_pr, "revision-a"),
        (original_editor, PublicationStage.tenant_draft_pr, "revision-a"),
        (replacement_editor, PublicationStage.tenant_draft_pr, "revision-a"),
        (replacement_editor, PublicationStage.tenant_draft_pr, "revision-b"),
    ]


@pytest.mark.asyncio
async def test_runtime_probe_never_shares_a_result_between_two_editors(
    executable_runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two editors never share a probe result, even on an identity collision.

    The cache keys on id(editor), which CPython reuses after collection. The
    strong editor reference on every stored entry is what makes that safe, and
    the identity re-check is the second line of defence. Forcing both editors
    onto one key proves neither can answer for the other.
    """
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    ready_editor = object()
    unavailable_editor = object()
    probed: list[object] = []

    def selective_runtime(editor: object, *_args: object) -> None:
        probed.append(editor)
        if editor is unavailable_editor:
            raise RuntimeError("worker image is not runnable")

    monkeypatch.setattr(capabilities, "_assert_runtime_ready", selective_runtime)
    monkeypatch.setattr(capabilities, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        capabilities,
        "codegen_revision",
        lambda: "revision-identity-collision",
    )
    # Collapse both editors onto one cache key. Without the strong reference
    # and the identity re-check, the second editor would read the first's
    # cached answer.
    monkeypatch.setattr(
        capabilities,
        "id",
        lambda _editor: 4_158_044_085,
        raising=False,
    )

    app.state.job_deps["editor"] = ready_editor
    first = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    app.state.job_deps["editor"] = unavailable_editor
    second = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    app.state.job_deps["editor"] = ready_editor
    third = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    assert probed == [ready_editor, unavailable_editor, ready_editor]
    assert first.report.changeset_creation == "available"
    assert first.report.checks.runtime == "ready"
    assert second.report.changeset_creation == "disabled"
    assert second.report.checks.runtime == "blocked"
    assert second.report.reasons == ["runtime_unavailable"]
    assert third.report.changeset_creation == "available"
    assert third.report.checks.runtime == "ready"


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


def test_provider_check_requires_codegen_credential_encryption_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
    assert capabilities._provider_configured() is False

    encoded_key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, encoded_key)
    assert capabilities._provider_configured() is True


@pytest.mark.parametrize(
    "encoded_key",
    [
        "",
        "not-base64",
        base64.b64encode(b"short").decode("ascii"),
        base64.urlsafe_b64encode(b"\xfb" * 32).decode("ascii"),
    ],
)
def test_provider_check_rejects_invalid_encryption_keys(
    encoded_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENCRYPTION_KEY_ENV, encoded_key)
    assert capabilities._provider_configured() is False


def test_deployment_provider_keys_are_not_tenant_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
    monkeypatch.setenv("CODEGEN_MODEL", "anthropic/claude-opus-5")
    monkeypatch.setenv("CODEGEN_HELPER_MODEL", "openai/gpt-5.4-mini")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")

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


@pytest.mark.asyncio
async def test_null_editor_is_not_worker_ready(executable_runtime) -> None:
    del executable_runtime
    pool = FakePool()
    pool.add_connection("demo")
    app.state.job_deps["editor"] = None

    evaluation = await capabilities.evaluate_changeset_creation(app, pool, "demo")

    assert evaluation.report.changeset_creation == "disabled"
    assert evaluation.report.reasons == ["worker_unavailable", "runtime_unavailable"]
