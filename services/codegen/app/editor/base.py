"""The editing-engine seam.

``Editor`` is the interface codegen uses to turn a task spec into a pushed
branch. Production uses an Aider-backed implementation (model-agnostic via
LiteLLM); tests use a fake. Keeping the engine behind a Protocol makes the engine
— and the model — a config choice, not a rewrite (plan decision D3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class EditRequest:
    """Everything the engine needs to implement one change and push a branch."""

    repo: str  # owner/name
    base_branch: str
    branch: str  # branch the engine must create and push
    token: str  # short-lived installation token, scoped to this repo
    title: str
    spec: str
    constraints: list[str] = field(default_factory=list)
    #: Repo test command for the agent's test loop + the post-edit verify.
    #: ``None`` lets the engine auto-detect from the repo (e.g. pytest/npm test).
    test_cmd: str | None = None


@dataclass
class EditResult:
    """Outcome of one edit attempt."""

    success: bool
    branch: str | None = None
    diff_stat: dict[str, Any] = field(default_factory=dict)
    changed_paths: list[str] = field(default_factory=list)
    diff_text: str = ""
    error: str | None = None
    logs_uri: str | None = None


class Editor(Protocol):
    """Implements a change and pushes a branch.

    Implementations MUST NOT raise for an ordinary failed attempt (tests not
    passing, budget exhausted) — return ``EditResult(success=False, error=...)``
    so the job can record a clean ``tests_failed``. Reserve exceptions for
    genuinely unexpected faults.
    """

    async def implement(self, request: EditRequest) -> EditResult: ...
