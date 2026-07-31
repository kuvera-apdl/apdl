"""Focused lifecycle contracts for project-scoped LLM routing."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest

from app.editor import routed as routed_module
from app.editor.base import EditRequest, EditResult
from app.editor.routed import ProjectRoutedEditor
from app.llm import broker as broker_module
from app.llm.broker import LlmAttemptBroker, LlmBrokerClient, _AcquireRequest
from app.llm.broker_directory import prepare_broker_root
from app.llm.contracts import (
    LlmAssignmentSnapshot,
    LlmExecutionAuthority,
    LlmExecutionSnapshot,
    LlmRuntimeBinding,
    PreparedLlmAttempt,
)
from app.models.execution import PublicationStage
from app.store import llm_routing


ATTEMPT_ID = UUID("30000000-0000-4000-8000-000000000003")
SECRET = "provider-secret-that-must-remain-ephemeral"
ROOT = Path(__file__).resolve().parents[3]


def _attempt() -> PreparedLlmAttempt:
    return PreparedLlmAttempt(
        attempt_id=ATTEMPT_ID,
        changeset_id="changeset-1",
        project_id="demo",
        phase="edit",
        attempt_sequence=1,
        binding=LlmRuntimeBinding(
            role="editor",
            provider="openai",
            model_id="gpt-5.4-mini",
            litellm_model="openai/gpt-5.4-mini",
            credential_environment_name="OPENAI_API_KEY",
            endpoint_url="https://api.openai.com/v1",
            assignment_version=1,
            credential_id=UUID(
                "40000000-0000-4000-8000-000000000004"
            ),
            credential_version=1,
            input_cost_per_million_tokens_usd_micros=250_000,
            output_cost_per_million_tokens_usd_micros=2_000_000,
            api_key=SECRET,
        ),
    )


def _assignment(role: str) -> LlmAssignmentSnapshot:
    return LlmAssignmentSnapshot(
        schema_version="codegen_llm_assignment_snapshot@1",
        role=role,
        provider="openai",
        model_id="gpt-5.4-mini",
        assignment_version=1,
        connection_version=1,
        inventory_version=1,
        catalog_version="codegen-provider-catalog@1",
        context_window_tokens=400_000,
        supports_tool_calling=True,
        supports_structured_output=True,
        input_cost_per_million_tokens_usd_micros=250_000,
        output_cost_per_million_tokens_usd_micros=2_000_000,
    )


def _execution_snapshot() -> LlmExecutionSnapshot:
    return LlmExecutionSnapshot(
        schema_version="codegen_llm_execution_snapshot@2",
        project_id="demo",
        repository_grant_id="ghg_test",
        repository_id=1,
        repository_installation_id=2,
        repository_full_name="acme/widgets",
        codegen_revision="test-revision",
        behavior_configuration_sha256="a" * 64,
        rollout_stage=PublicationStage.offline.value,
        assignments=(_assignment("editor"), _assignment("helper")),
    )


@pytest.mark.asyncio
async def test_prepared_cleanup_is_a_repeated_cancellation_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    terminalized = asyncio.Event()
    calls: list[tuple[UUID, bool]] = []

    async def abandon(
        pool: object,
        *,
        attempt_id: UUID,
        cancelled: bool,
    ) -> None:
        assert pool is fake_pool
        calls.append((attempt_id, cancelled))
        cleanup_entered.set()
        await cleanup_release.wait()
        terminalized.set()

    monkeypatch.setattr(llm_routing, "abandon_llm_attempt", abandon)
    fake_pool = object()
    caller_started = asyncio.Event()
    never = asyncio.Event()

    async def cancelled_caller() -> None:
        caller_started.set()
        try:
            await never.wait()
        except BaseException:
            await llm_routing._cleanup_interrupted_prepared_attempt(
                fake_pool,
                attempt_id=ATTEMPT_ID,
                cancelled=True,
            )
            raise

    caller = asyncio.create_task(cancelled_caller())
    await caller_started.wait()
    caller.cancel()
    await cleanup_entered.wait()

    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()
    assert not terminalized.is_set()
    assert calls == [(ATTEMPT_ID, True)]

    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert terminalized.is_set()


@pytest.mark.asyncio
async def test_broker_acquire_terminalizes_registered_attempt_before_unwind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared = _attempt()
    mark_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    terminalized = asyncio.Event()

    async def prepare(*args: object, **kwargs: object) -> PreparedLlmAttempt:
        return prepared

    async def mark(*args: object, **kwargs: object) -> None:
        mark_entered.set()
        await asyncio.Event().wait()

    async def abandon(
        pool: object,
        *,
        attempt_id: UUID,
        cancelled: bool,
    ) -> None:
        assert attempt_id == ATTEMPT_ID
        assert cancelled is True
        cleanup_entered.set()
        await cleanup_release.wait()
        terminalized.set()

    monkeypatch.setattr(broker_module, "prepare_llm_attempt", prepare)
    monkeypatch.setattr(broker_module, "mark_llm_egress", mark)
    monkeypatch.setattr(broker_module, "abandon_llm_attempt", abandon)
    broker = LlmAttemptBroker(
        pool=object(),
        credential_store=object(),  # type: ignore[arg-type]
        changeset_id="changeset-1",
        socket_path=tmp_path / "broker.sock",
        token="t" * 43,
        allowed_phases=("brief", "edit", "review"),
    )
    request = _AcquireRequest(
        schema_version="codegen_llm_broker_request@1",
        action="acquire",
        token="t" * 43,
        changeset_id="changeset-1",
        phase="edit",
    )

    acquire = asyncio.create_task(broker._acquire(request))
    await mark_entered.wait()
    acquire.cancel()
    await cleanup_entered.wait()
    acquire.cancel()
    await asyncio.sleep(0)

    assert not acquire.done()
    assert not terminalized.is_set()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await acquire
    assert terminalized.is_set()
    assert broker._active == {}


@pytest.mark.asyncio
async def test_broker_unix_socket_acquire_finish_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _attempt()
    marked: list[PreparedLlmAttempt] = []
    finished: list[dict[str, object]] = []

    async def prepare(*args: object, **kwargs: object) -> PreparedLlmAttempt:
        assert kwargs["changeset_id"] == "changeset-1"
        assert kwargs["phase"] == "edit"
        return prepared

    async def mark(
        pool: object,
        *,
        attempt: PreparedLlmAttempt,
    ) -> None:
        marked.append(attempt)

    async def finish(pool: object, **kwargs: object) -> None:
        finished.append(kwargs)

    async def abandon(*args: object, **kwargs: object) -> None:
        raise AssertionError("finished round trip must not abandon its attempt")

    monkeypatch.setattr(broker_module, "prepare_llm_attempt", prepare)
    monkeypatch.setattr(broker_module, "mark_llm_egress", mark)
    monkeypatch.setattr(broker_module, "finish_llm_attempt", finish)
    monkeypatch.setattr(broker_module, "abandon_llm_attempt", abandon)
    with TemporaryDirectory(prefix="apdl-broker-", dir="/tmp") as directory:
        broker_root = Path(directory) / "broker-root"
        prepare_broker_root(broker_root)
        socket_path = broker_root / ("a" * 24) / "broker.sock"
        token = "t" * 43
        broker = LlmAttemptBroker(
            pool=object(),
            credential_store=object(),  # type: ignore[arg-type]
            changeset_id="changeset-1",
            socket_path=socket_path,
            token=token,
            allowed_phases=("brief", "edit", "review"),
        )
        authority = LlmExecutionAuthority(
            socket_path=socket_path.as_posix(),
            token=token,
            editor_model="openai/gpt-5.4-mini",
            helper_model="openai/gpt-5.4-nano",
            allowed_phases=("brief", "edit", "review"),
        )
        client = LlmBrokerClient(authority, "changeset-1")

        await broker.start()
        try:
            lease, started_at = await client.acquire("edit")
            assert lease.attempt_id == ATTEMPT_ID
            assert isinstance(lease.attempt_id, UUID)
            assert isinstance(lease.binding.credential_id, UUID)

            await client.finish(
                lease,
                started_at,
                status="succeeded",
                error_classification=None,
                input_tokens=10,
                output_tokens=20,
            )
        finally:
            await broker.close()

    assert marked == [prepared]
    assert finished == [
        {
            "attempt_id": ATTEMPT_ID,
            "status": "succeeded",
            "latency_ms": 0,
            "input_tokens": 10,
            "output_tokens": 20,
            "cost_usd_micros": 43,
            "error_classification": None,
        }
    ]


@pytest.mark.asyncio
async def test_broker_start_never_recreates_an_unprepared_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing-root"
    broker = LlmAttemptBroker(
        pool=object(),
        credential_store=object(),  # type: ignore[arg-type]
        changeset_id="changeset-1",
        socket_path=root / ("a" * 24) / "broker.sock",
        token="t" * 43,
        allowed_phases=("edit",),
    )

    with pytest.raises(RuntimeError, match="must be prepared before serving"):
        await broker.start()

    assert not root.exists()


@pytest.mark.asyncio
async def test_broker_start_rejects_a_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o711)
    target.chmod(0o711)
    root = tmp_path / "broker-root"
    root.symlink_to(target, target_is_directory=True)
    broker = LlmAttemptBroker(
        pool=object(),
        credential_store=object(),  # type: ignore[arg-type]
        changeset_id="changeset-1",
        socket_path=root / ("a" * 24) / "broker.sock",
        token="t" * 43,
        allowed_phases=("edit",),
    )

    with pytest.raises(RuntimeError, match="must be a real directory"):
        await broker.start()

    assert list(target.iterdir()) == []


@pytest.mark.asyncio
async def test_broker_child_creation_is_pinned_across_parent_alias_swap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="apdl-broker-race-", dir="/tmp") as directory:
        base = Path(directory)
        original_parent = base / "original"
        alternate_parent = base / "alternate"
        original_parent.mkdir(mode=0o700)
        alternate_parent.mkdir(mode=0o700)
        lexical_parent = base / "alias"
        lexical_parent.symlink_to(original_parent, target_is_directory=True)
        root = lexical_parent / "root"
        prepare_broker_root(root)
        alternate_root = alternate_parent / root.name
        prepare_broker_root(alternate_root)
        child_name = "b" * 24
        socket_path = root / child_name / "broker.sock"
        real_mkdir = os.mkdir
        swapped = False

        def swap_then_mkdir(
            name: str,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if name == child_name and dir_fd is not None and not swapped:
                lexical_parent.unlink()
                lexical_parent.symlink_to(
                    alternate_parent,
                    target_is_directory=True,
                )
                swapped = True
            real_mkdir(name, mode=mode, dir_fd=dir_fd)

        monkeypatch.setattr(broker_module.os, "mkdir", swap_then_mkdir)
        broker = LlmAttemptBroker(
            pool=object(),
            credential_store=object(),  # type: ignore[arg-type]
            changeset_id="changeset-1",
            socket_path=socket_path,
            token="t" * 43,
            allowed_phases=("edit",),
        )

        await broker.start()
        try:
            assert swapped is True
            assert (
                original_parent / root.name / child_name / "broker.sock"
            ).is_socket()
            assert not (alternate_root / child_name).exists()
        finally:
            await broker.close()

        assert not (original_parent / root.name / child_name).exists()


@pytest.mark.asyncio
async def test_routed_editor_closes_broker_before_repeated_cancel_unwinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor_entered = asyncio.Event()
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    closed = asyncio.Event()
    close_arguments: list[bool] = []

    class WaitingEditor:
        async def implement(self, request: EditRequest) -> EditResult:
            assert request.llm_execution is not None
            editor_entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class HeldBroker:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def start(self) -> None:
            return None

        async def close(self, *, cancelled: bool = False) -> None:
            close_arguments.append(cancelled)
            close_entered.set()
            await close_release.wait()
            closed.set()

    async def load(
        pool: object,
        changeset_id: str,
    ) -> LlmExecutionSnapshot:
        assert changeset_id == "changeset-1"
        return _execution_snapshot()

    monkeypatch.setattr(routed_module, "load_execution_snapshot", load)
    monkeypatch.setattr(routed_module, "LlmAttemptBroker", HeldBroker)
    editor = ProjectRoutedEditor(
        WaitingEditor(),
        pool=object(),
        credential_store=object(),  # type: ignore[arg-type]
    )
    request = EditRequest(
        repo="acme/widgets",
        changeset_id="changeset-1",
        project_scope="demo",
        base_branch="main",
        branch="apdl/change",
        token="installation-token",
        title="Change",
        spec="Make the change",
    )

    implementation = asyncio.create_task(editor.implement(request))
    await editor_entered.wait()
    implementation.cancel()
    await close_entered.wait()
    implementation.cancel()
    await asyncio.sleep(0)

    assert not implementation.done()
    assert not closed.is_set()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await implementation
    assert closed.is_set()
    assert close_arguments == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_scope", "repo"),
    [
        ("other-project", "acme/widgets"),
        ("demo", "other/repository"),
    ],
)
async def test_routed_editor_rejects_request_snapshot_tenant_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    project_scope: str,
    repo: str,
) -> None:
    class UnexpectedEditor:
        async def implement(self, request: EditRequest) -> EditResult:
            raise AssertionError("mismatched request must not reach editor")

    class UnexpectedBroker:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("mismatched request must not create broker")

    async def load(
        pool: object,
        changeset_id: str,
    ) -> LlmExecutionSnapshot:
        assert changeset_id == "changeset-1"
        return _execution_snapshot()

    monkeypatch.setattr(routed_module, "load_execution_snapshot", load)
    monkeypatch.setattr(routed_module, "LlmAttemptBroker", UnexpectedBroker)
    editor = ProjectRoutedEditor(
        UnexpectedEditor(),
        pool=object(),
        credential_store=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        ValueError,
        match="request does not match the changeset execution snapshot",
    ):
        await editor.implement(
            EditRequest(
                repo=repo,
                changeset_id="changeset-1",
                project_scope=project_scope,
                base_branch="main",
                branch="apdl/change",
                token="installation-token",
                title="Change",
                spec="Make the change",
            )
        )


def test_snapshot_sql_requires_canonical_integer_json_numbers() -> None:
    assignment_migration = (
        ROOT
        / "pipeline/postgres/migrations"
        / "054_codegen_project_llm_routing.sql"
    ).read_text(encoding="utf-8")
    snapshot_migration = (
        ROOT
        / "pipeline/postgres/migrations"
        / "055_codegen_tenant_publication.sql"
    ).read_text(encoding="utf-8")

    for field in (
        "assignment_version",
        "connection_version",
        "inventory_version",
        "context_window_tokens",
    ):
        assert f"->>'{field}' ~ '^[1-9][0-9]*$'" in assignment_migration
    for field in (
        "repository_id",
        "repository_installation_id",
    ):
        assert f"->>'{field}' ~ '^[1-9][0-9]*$'" in snapshot_migration
    for field in (
        "input_cost_per_million_tokens_usd_micros",
        "output_cost_per_million_tokens_usd_micros",
    ):
        assert (
            f"assignment->>'{field}'\n"
            "        ) ~ '^(0|[1-9][0-9]*)$'"
        ) in assignment_migration


def test_snapshot_runtime_accepts_tenant_assignments_and_rejects_deployment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _execution_snapshot().model_copy(
        update={"rollout_stage": PublicationStage.tenant_draft_pr.value}
    )
    monkeypatch.setenv(
        "CODEGEN_ROLLOUT_STAGE",
        PublicationStage.tenant_draft_pr.value,
    )
    monkeypatch.setenv("CODEGEN_REVISION", "test-revision")
    monkeypatch.setattr(
        llm_routing,
        "codegen_tenant_behavior_configuration_sha256",
        lambda: "a" * 64,
    )

    assert llm_routing._snapshot_runtime_is_current(snapshot) is True

    tenant_selected = snapshot.model_copy(
        update={
            "assignments": (
                snapshot.assignment("editor"),
                snapshot.assignment("helper").model_copy(
                    update={"model_id": "gpt-5.4-nano"}
                ),
            )
        }
    )
    assert llm_routing._snapshot_runtime_is_current(tenant_selected) is True
    stale_revision = tenant_selected.model_copy(
        update={"codegen_revision": "previous-revision"}
    )
    assert llm_routing._snapshot_runtime_is_current(stale_revision) is False

    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", PublicationStage.offline.value)
    assert llm_routing._snapshot_runtime_is_current(tenant_selected) is False
