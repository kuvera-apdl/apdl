"""Codegen service tools — wrappers around the Codegen Service API.

The agents service never touches git, credentials, or the sandbox directly; it
asks the codegen service (`:8084`) to produce and manage changesets, exactly as
the flag tools defer to the config service. Merge gating and approval stay here
in the brain; codegen is purely the hands.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

CODEGEN_SERVICE_URL = os.getenv("CODEGEN_SERVICE_URL", "http://localhost:8084")
_TIMEOUT = 30.0


def _seg(value: str) -> str:
    """URL-quote one path segment — ids here are often LLM-authored, and an id
    containing '/' or '?' would otherwise reroute the request."""
    return quote(value, safe="")


def _headers() -> dict[str, str]:
    token = os.getenv("APDL_INTERNAL_TOKEN", "")
    return {"X-APDL-Internal-Token": token} if token else {}


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=CODEGEN_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get(path, params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=CODEGEN_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post(path, json=payload, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def open_changeset(
    project_id: str,
    title: str,
    spec: str,
    *,
    run_id: str | None = None,
    base_branch: str | None = None,
    context: dict[str, Any] | None = None,
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    """Ask codegen to produce a changeset (branch + edits + draft PR) for a task.

    Returns the changeset record (includes ``changeset_id`` and ``status``).
    """
    payload: dict[str, Any] = {
        "project_id": project_id,
        "task": {
            "title": title,
            "spec": spec,
            "context": context or {},
            "constraints": constraints or [],
        },
    }
    if run_id is not None:
        payload["run_id"] = run_id
    if base_branch is not None:
        payload["base_branch"] = base_branch
    return await _post("/v1/changesets", payload)


async def get_changeset(changeset_id: str) -> dict[str, Any]:
    """Fetch a changeset's current status (incl. ``pr_url`` and ``ci_status``)."""
    return await _get(f"/v1/changesets/{_seg(changeset_id)}")


async def get_repo_context(project_id: str) -> dict[str, Any]:
    """Compact facts about the project's connected repo (stack, layout, scripts).

    Grounds the feature-proposal prompt in what the repository actually is, so
    specs name real files and stay inside the repo's capabilities instead of
    demanding infrastructure it does not have.
    """
    return await _get(f"/v1/connections/{_seg(project_id)}/repo-context")


async def list_changesets(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """List the project's changesets (newest first), incl. task title + PR state."""
    return await _get("/v1/changesets", params={"project_id": project_id, "limit": limit})


async def merge_changeset(changeset_id: str, merge_method: str = "squash") -> dict[str, Any]:
    """Merge a changeset's PR. codegen enforces green CI; APDL gates the call."""
    return await _post(f"/v1/changesets/{_seg(changeset_id)}/merge", {"merge_method": merge_method})


async def abandon_changeset(changeset_id: str) -> dict[str, Any]:
    """Abandon a changeset (close PR / drop branch) — rollback for un-merged work."""
    return await _post(f"/v1/changesets/{_seg(changeset_id)}/abandon")


async def revert_changeset(changeset_id: str) -> dict[str, Any]:
    """Roll back a merged changeset by opening a revert PR (a new changeset)."""
    return await _post(f"/v1/changesets/{_seg(changeset_id)}/revert")
