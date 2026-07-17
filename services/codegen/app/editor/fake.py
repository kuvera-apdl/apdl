"""Deterministic Editor for tests."""

from __future__ import annotations

import base64
from dataclasses import replace

from app.editor.base import EditRequest, EditResult
from app.requirements import compile_requirement_ledger, map_implementation_evidence


def _ledger(request: EditRequest, result: EditResult):
    if result.requirement_ledger is not None:
        return result.requirement_ledger
    compiled = compile_requirement_ledger(
        title=request.title,
        spec=request.spec,
        constraints=request.constraints,
        risk=request.risk_level,
        verification_command=request.test_cmd,
    )
    return map_implementation_evidence(
        compiled, result.changed_paths or ["generated-change"]
    )


class FakeEditor:
    """Returns a canned result and records the last request it saw."""

    def __init__(self, result: EditResult | None = None) -> None:
        self._result = result or EditResult(
            success=True,
            branch=None,
            diff_stat={"files": 1, "additions": 10, "deletions": 0},
        )
        self.last_request: EditRequest | None = None

    async def implement(self, request: EditRequest) -> EditResult:
        self.last_request = request
        result = self._result
        if result.success:
            return replace(
                result,
                branch=result.branch or request.branch,
                head_sha=result.head_sha or "c" * 40,
                base_sha=result.base_sha or "a" * 40,
                candidate_tree_sha=result.candidate_tree_sha or "b" * 40,
                patch_base64=(
                    result.patch_base64
                    or base64.b64encode(b"fake candidate patch").decode("ascii")
                ),
                requirement_ledger=_ledger(request, result),
            )
        return result
