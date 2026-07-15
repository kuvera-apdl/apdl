"""Deterministic Editor for tests."""

from __future__ import annotations

from app.editor.base import EditRequest, EditResult


class FakeEditor:
    """Returns a canned result and records the last request it saw."""

    def __init__(self, result: EditResult | None = None) -> None:
        self._result = result or EditResult(
            success=True, branch=None, diff_stat={"files": 1, "additions": 10}
        )
        self.last_request: EditRequest | None = None

    async def implement(self, request: EditRequest) -> EditResult:
        self.last_request = request
        result = self._result
        # Default a successful result's branch to the requested one.
        if result.success and result.branch is None:
            return EditResult(
                success=True,
                branch=request.branch,
                diff_stat=result.diff_stat,
                changed_paths=result.changed_paths,
                diff_text=result.diff_text,
                prompts=result.prompts,
            )
        return result
