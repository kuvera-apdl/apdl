"""Deterministic Editor for tests."""

from __future__ import annotations

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
                head_sha=result.head_sha,
                contract_bundle=result.contract_bundle,
                requirement_ledger=_ledger(request, result),
                inspection_snapshot=result.inspection_snapshot,
                dependency_slice=result.dependency_slice,
            )
        return result
