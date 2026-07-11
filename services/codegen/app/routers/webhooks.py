"""GitHub webhook triggers resolved by repository plus PR number or head SHA."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.config import github_webhook_secret
from app.jobs.ci import sync_github_state
from app.store import changesets as changeset_store

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
_PR_ACTIONS = {
    "opened",
    "ready_for_review",
    "converted_to_draft",
    "synchronize",
    "closed",
    "reopened",
}


def _signature_valid(body: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _head_sha(event: str, payload: dict[str, Any]) -> str:
    if event == "check_run":
        run = payload.get("check_run") or {}
        return str(run.get("head_sha") or (run.get("check_suite") or {}).get("head_sha") or "")
    if event == "check_suite":
        return str((payload.get("check_suite") or {}).get("head_sha") or "")
    if event == "status":
        return str(payload.get("sha") or "")
    return ""


@router.post("/github")
async def github_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Verify a webhook and use it only as a trigger for live GitHub recovery."""
    body = await request.body()
    secret = github_webhook_secret()
    if secret and not _signature_valid(
        body, request.headers.get("X-Hub-Signature-256", ""), secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed webhook body.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook body must be an object.")

    event = request.headers.get("X-GitHub-Event", "")
    repository = str((payload.get("repository") or {}).get("full_name") or "")
    if not repository:
        return {"status": "ignored"}
    pool = request.app.state.pg_pool
    action = "polled"
    delivery_id: str | None = None

    if event == "pull_request":
        action = str(payload.get("action") or "")
        if action not in _PR_ACTIONS:
            return {"status": "ignored"}
        number = payload.get("number") or (payload.get("pull_request") or {}).get(
            "number"
        )
        if not isinstance(number, int):
            return {"status": "ignored"}
        delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
        if not delivery_id:
            raise HTTPException(
                status_code=400,
                detail="GitHub pull-request webhook is missing its delivery ID.",
            )
        changeset = await changeset_store.get_changeset_by_pr_number(
            pool, number, repository
        )
    elif event in {"check_run", "check_suite", "status"}:
        head_sha = _head_sha(event, payload)
        if not head_sha:
            return {"status": "ignored"}
        changeset = await changeset_store.get_changeset_by_head_sha(
            pool, head_sha, repository
        )
    else:
        return {"status": "ignored"}

    if changeset is None:
        return {"status": "no_changeset"}
    deps = getattr(request.app.state, "github_sync_deps", None)
    if not deps:
        return {"status": "no_github_sync"}
    background_tasks.add_task(
        sync_github_state,
        pool,
        changeset.changeset_id,
        **deps,
        pr_action=action,
        delivery_id=delivery_id,
    )
    return {"status": "queued", "changeset_id": changeset.changeset_id}
