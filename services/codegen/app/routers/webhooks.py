"""GitHub webhook receiver — ingests CI status so merges can be gated on it.

GitHub POSTs ``check_run`` / ``check_suite`` / ``pull_request`` / ``status``
events here; the body is HMAC-verified (when a secret is configured), the target
changeset is resolved by branch, and a CI-status sync is scheduled. The sync
dependencies live on ``app.state.ci_deps`` (wired in lifespan), so this router
is inert in tests unless they configure it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import github_webhook_secret
from app.jobs.ci import sync_ci_status
from app.store.changesets import get_changeset_by_branch

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _signature_valid(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _branch_of(event: str, payload: dict[str, Any]) -> str:
    if event == "check_run":
        return payload.get("check_run", {}).get("check_suite", {}).get("head_branch", "")
    if event == "check_suite":
        return payload.get("check_suite", {}).get("head_branch", "")
    if event == "pull_request":
        return payload.get("pull_request", {}).get("head", {}).get("ref", "")
    if event == "status":
        branches = payload.get("branches", [])
        return branches[0].get("name", "") if branches else ""
    return ""


@router.post("/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Verify, route, and schedule a CI sync for a GitHub event."""
    body = await request.body()

    secret = github_webhook_secret()
    if secret and not _signature_valid(
        body, request.headers.get("X-Hub-Signature-256", ""), secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    event = request.headers.get("X-GitHub-Event", "")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook body.") from None

    branch = _branch_of(event, payload)
    if not branch:
        return {"status": "ignored"}

    pool = request.app.state.pg_pool
    changeset = await get_changeset_by_branch(pool, branch)
    if changeset is None:
        return {"status": "no_changeset"}

    deps = getattr(request.app.state, "ci_deps", None)
    if not deps:
        return {"status": "no_ci_runner"}

    background_tasks.add_task(sync_ci_status, pool, changeset.changeset_id, **deps)
    return {"status": "queued", "changeset_id": changeset.changeset_id}
