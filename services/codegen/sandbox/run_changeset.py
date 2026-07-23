"""In-sandbox changeset runner — the ENTRYPOINT of the codegen sandbox image.

This is how the editor runs under ``CODEGEN_SANDBOX=docker`` (decision D4 /
Option B). The orchestrator (``app.editor.container_editor.ContainerAiderEditor``)
launches one ephemeral container per changeset from the hardened sandbox image
and this script runs *inside* it. It reuses the very same ``AiderEditor`` used by
the in-process path — clone → Aider → gate → candidate patch — so the edit logic
lives in exactly one place; the only difference is *where* it runs. The service
controller reconstructs and publishes the exact returned tree.

Contract with the orchestrator: emit the ``EditResult`` as a single JSON object
on **stdout** and send everything else (logs and Aider output) to **stderr**,
so stdout stays cleanly parseable.

Input custody: one strict, bounded ``codegen_worker_request@1`` JSON object
arrives on stdin. Task text and the short-lived read-only installation token
therefore never enter process arguments or environment. ``AiderEditor`` uses
the token only for the one-shot clone header. The worker never receives
repository write authority; the sandbox also never receives the GitHub App
private key, Postgres DSN, or internal token.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys

from app.editor.aider_editor import AiderEditor
from app.editor.base import EditResult
from app.editor.worker_contract import (
    CodegenWorkerRequestError,
    read_codegen_worker_request,
)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), stream=sys.stderr)
    try:
        request = read_codegen_worker_request(sys.stdin.buffer).to_edit_request()
    except CodegenWorkerRequestError as exc:
        print(
            json.dumps({"success": False, "error": f"invalid sandbox input: {exc}"})
        )
        return 1

    result: EditResult = asyncio.run(AiderEditor().implement(request))
    # The result is data, not a status — a clean "tests failed" is still exit 0.
    payload = dataclasses.asdict(result)
    if result.contract_bundle is not None:
        payload["contract_bundle"] = result.contract_bundle.model_dump(mode="json")
    if result.requirement_ledger is not None:
        payload["requirement_ledger"] = result.requirement_ledger.model_dump(
            mode="json"
        )
    if result.inspection_snapshot is not None:
        payload["inspection_snapshot"] = result.inspection_snapshot.model_dump(
            mode="json"
        )
    if result.dependency_slice is not None:
        payload["dependency_slice"] = result.dependency_slice.model_dump(mode="json")
    if result.verification_plan is not None:
        payload["verification_plan"] = result.verification_plan.model_dump(mode="json")
    if result.verification_coverage is not None:
        payload["verification_coverage"] = result.verification_coverage.model_dump(
            mode="json"
        )
    if result.runtime_acceptance_plan is not None:
        payload["runtime_acceptance_plan"] = result.runtime_acceptance_plan.model_dump(
            mode="json"
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
