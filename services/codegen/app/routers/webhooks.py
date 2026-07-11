"""GitHub webhook receiver for authoritative CI and PR observations.

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
from app.models.changeset import ChangesetStatus, InvalidTransition
from app.store import changesets as changeset_store

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

    repo = payload.get("repository", {}).get("full_name", "")
    if not repo:
        return {"status": "ignored"}

    pool = request.app.state.pg_pool
    if event == "pull_request" and payload.get("action") == "closed":
        pr = payload.get("pull_request") or {}
        number = pr.get("number") or payload.get("number")
        if not isinstance(number, int):
            return {"status": "ignored"}
        changeset = await changeset_store.get_changeset_by_pr_number(pool, number, repo)
        if changeset is None:
            return {"status": "no_changeset"}
        try:
            if pr.get("merged") is True:
                await changeset_store.mark_merged(
                    pool,
                    changeset.changeset_id,
                    merge_sha=str(pr.get("merge_commit_sha") or ""),
                )
                return {"status": "observed_merged", "changeset_id": changeset.changeset_id}
            await changeset_store.transition_changeset(
                pool, changeset.changeset_id, ChangesetStatus.abandoned
            )
            return {"status": "observed_closed", "changeset_id": changeset.changeset_id}
        except InvalidTransition:
            return {"status": "ignored"}

    branch = _branch_of(event, payload)
    if not branch:
        return {"status": "ignored"}
    # Scope by repo as well as branch so two connected repos sharing a branch
    # name can't mis-route each other's CI events.
    changeset = await changeset_store.get_changeset_by_branch(pool, branch, repo)
    if changeset is None:
        return {"status": "no_changeset"}

    deps = getattr(request.app.state, "ci_deps", None)
    if not deps:
        return {"status": "no_ci_runner"}

    background_tasks.add_task(sync_ci_status, pool, changeset.changeset_id, **deps)
    return {"status": "queued", "changeset_id": changeset.changeset_id}
