"""Controller-owned per-call project LLM authority for editor execution."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import replace
from pathlib import Path

from app.config import codegen_llm_broker_dir
from app.editor.base import Editor, EditRequest, EditResult
from app.llm.broker import LlmAttemptBroker
from app.llm.contracts import LlmExecutionAuthority
from app.llm.provider_catalog import runtime_model
from app.store.llm_credentials import ProjectCredentialStore
from app.store.llm_routing import load_execution_snapshot


async def _close_broker_barrier(
    broker: LlmAttemptBroker,
    *,
    cancelled: bool,
    preserve_primary_error: bool,
) -> None:
    """Finish broker cleanup despite repeated cancellation of the caller."""
    cleanup = asyncio.create_task(broker.close(cancelled=cancelled))
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
            continue
        except Exception:
            if preserve_primary_error:
                return
            raise
    try:
        cleanup.result()
    except BaseException:
        if preserve_primary_error:
            return
        raise
    if interrupted and not preserve_primary_error:
        raise asyncio.CancelledError


class ProjectRoutedEditor:
    """Expose one just-in-time plaintext credential per worker phase attempt."""

    def __init__(
        self,
        editor: Editor,
        *,
        pool: object,
        credential_store: ProjectCredentialStore,
    ) -> None:
        self._editor = editor
        self._pool = pool
        self._credentials = credential_store

    def assert_runtime_ready(self, **kwargs: object) -> None:
        """Delegate non-tenant worker attestation without resolving credentials."""
        check = getattr(self._editor, "assert_runtime_ready", None)
        if check is None:
            raise RuntimeError("Editor runtime does not expose readiness attestation")
        check(**kwargs)

    async def implement(self, request: EditRequest) -> EditResult:
        """Run a worker with a changeset-bound broker, not standing secrets."""
        changeset_id = request.changeset_id
        if not changeset_id:
            raise ValueError(
                "Project-routed editor requires a durable changeset identity"
            )
        snapshot = await load_execution_snapshot(self._pool, changeset_id)
        if (
            request.project_scope != snapshot.project_id
            or request.repo != snapshot.repository_full_name
        ):
            raise ValueError(
                "Project-routed editor request does not match the changeset "
                "execution snapshot"
            )
        editor_assignment = snapshot.assignment("editor")
        helper_assignment = snapshot.assignment("helper")
        editor_model = runtime_model(
            editor_assignment.provider,
            editor_assignment.model_id,
        ).litellm_model
        helper_model = runtime_model(
            helper_assignment.provider,
            helper_assignment.model_id,
        ).litellm_model
        editor_phase = "repair" if request.existing_branch else "edit"
        allowed_phases = ("brief", editor_phase, "review")

        root = Path(codegen_llm_broker_dir())
        socket_path = root / secrets.token_hex(12) / "broker.sock"
        token = secrets.token_urlsafe(32)
        authority = LlmExecutionAuthority(
            socket_path=str(socket_path),
            token=token,
            editor_model=editor_model,
            helper_model=helper_model,
            allowed_phases=allowed_phases,
        )
        broker = LlmAttemptBroker(
            pool=self._pool,
            credential_store=self._credentials,
            changeset_id=changeset_id,
            socket_path=socket_path,
            token=token,
            allowed_phases=allowed_phases,
        )
        await broker.start()
        cancelled = False
        primary_error: BaseException | None = None
        try:
            return await self._editor.implement(
                replace(request, llm_execution=authority)
            )
        except BaseException as exc:
            primary_error = exc
            cancelled = isinstance(exc, asyncio.CancelledError)
            raise
        finally:
            await _close_broker_barrier(
                broker,
                cancelled=cancelled,
                preserve_primary_error=primary_error is not None,
            )
