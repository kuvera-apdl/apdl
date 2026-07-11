"""In-sandbox changeset runner — the ENTRYPOINT of the codegen sandbox image.

This is how the editor runs under ``CODEGEN_SANDBOX=docker`` (decision D4 /
Option B). The orchestrator (``app.editor.container_editor.ContainerAiderEditor``)
launches one ephemeral container per changeset from the hardened sandbox image
and this script runs *inside* it. It reuses the very same ``AiderEditor`` used by
the in-process path — clone → aider → verify → push — so the edit logic lives in
exactly one place; the only difference is *where* it runs.

Contract with the orchestrator: emit the ``EditResult`` as a single JSON object
on **stdout** and send everything else (logs, aider/test output) to **stderr**,
so stdout stays cleanly parseable.

Token custody: the short-lived install token arrives as ``GH_TOKEN``. We read it
into the in-memory request and immediately drop it from ``os.environ`` so it is
not visible to the (untrusted) aider/test child processes — not even via
``/proc/<pid>/environ``. ``AiderEditor`` then uses it only for the one-shot
clone/push git header. The model provider key necessarily stays in the
environment (aider needs it); the sandbox never receives the GitHub App private
key, Postgres DSN, or internal token.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys

from app.editor.aider_editor import AiderEditor
from app.editor.base import EditRequest, EditResult
from app.requirements.models import RequirementLedger
from app.runtime.models import RuntimeAcceptancePlan, RuntimeAcceptancePolicy


def _request_from_env() -> EditRequest:
    """Build the EditRequest from the env the orchestrator passed via ``docker -e``."""
    request = EditRequest(
        repo=os.environ["CS_REPO"],
        project_scope=os.environ.get("CS_PROJECT_SCOPE", os.environ["CS_REPO"]),
        base_branch=os.environ["CS_BASE"],
        branch=os.environ["CS_BRANCH"],
        token=os.environ.get("GH_TOKEN", ""),
        title=os.environ.get("CS_TITLE", ""),
        spec=os.environ["CS_SPEC"],
        constraints=json.loads(os.environ.get("CS_CONSTRAINTS", "[]")),
        test_cmd=(os.environ.get("CS_TEST_CMD") or None),
        gates_policy=json.loads(os.environ.get("CS_GATES_POLICY") or "null"),
        revert_sha=(os.environ.get("CS_REVERT_SHA") or None),
        existing_branch=os.environ.get("CS_EXISTING_BRANCH") == "true",
        expected_head_sha=(os.environ.get("CS_EXPECTED_HEAD_SHA") or None),
        risk_level=os.environ.get("CS_RISK_LEVEL", "low"),
        requirement_ledger=(
            RequirementLedger.model_validate_json(os.environ["CS_REQUIREMENT_LEDGER"])
            if os.environ.get("CS_REQUIREMENT_LEDGER")
            else None
        ),
        runtime_acceptance_plan=(
            RuntimeAcceptancePlan.model_validate_json(
                os.environ["CS_RUNTIME_ACCEPTANCE_PLAN"]
            )
            if os.environ.get("CS_RUNTIME_ACCEPTANCE_PLAN")
            else None
        ),
        runtime_acceptance_policy=RuntimeAcceptancePolicy.model_validate_json(
            os.environ.get("CS_RUNTIME_ACCEPTANCE_POLICY", "{}")
        ),
    )
    # The token now lives only in `request`; keep it out of every child's view.
    os.environ.pop("GH_TOKEN", None)
    return request


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), stream=sys.stderr)
    try:
        request = _request_from_env()
    except (KeyError, ValueError) as exc:
        print(json.dumps({"success": False, "error": f"invalid sandbox input: {exc}"}))
        return 1

    result: EditResult = asyncio.run(AiderEditor().implement(request))
    # The result is data, not a status — a clean "tests failed" is still exit 0.
    payload = dataclasses.asdict(result)
    if result.contract_bundle is not None:
        payload["contract_bundle"] = result.contract_bundle.model_dump(mode="json")
    if result.requirement_ledger is not None:
        payload["requirement_ledger"] = result.requirement_ledger.model_dump(mode="json")
    if result.inspection_snapshot is not None:
        payload["inspection_snapshot"] = result.inspection_snapshot.model_dump(mode="json")
    if result.dependency_slice is not None:
        payload["dependency_slice"] = result.dependency_slice.model_dump(mode="json")
    if result.verification_plan is not None:
        payload["verification_plan"] = result.verification_plan.model_dump(mode="json")
    if result.verification_coverage is not None:
        payload["verification_coverage"] = result.verification_coverage.model_dump(
            mode="json"
        )
    if result.runtime_acceptance_plan is not None:
        payload["runtime_acceptance_plan"] = (
            result.runtime_acceptance_plan.model_dump(mode="json")
        )
    if result.generated_runtime_workflow is not None:
        payload["generated_runtime_workflow"] = (
            result.generated_runtime_workflow.model_dump(mode="json")
        )
    if result.review_verdict is not None:
        payload["review_verdict"] = result.review_verdict.model_dump(mode="json")
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
