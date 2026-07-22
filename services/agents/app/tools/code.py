"""Codegen service tools — wrappers around the Codegen Service API.

The agents service never touches git, credentials, or the sandbox directly; it
asks the codegen service (`:8084`) to produce and manage changesets, exactly as
the flag tools defer to the config service. GitHub owns CI verification and
merge; APDL only creates and observes pull requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.service_auth import service_headers

CODEGEN_SERVICE_URL = os.getenv("CODEGEN_SERVICE_URL", "http://localhost:8084")
_TIMEOUT = 30.0
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_IDEMPOTENCY_SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_CAPABILITY_REASONS = frozenset(
    {
        "rollout_stage_blocked",
        "automation_disabled",
        "repository_grant_missing",
        "github_app_unconfigured",
        "provider_unconfigured",
        "worker_unavailable",
        "runtime_unavailable",
    }
)
_CAPABILITY_CHECKS = frozenset(
    {
        "rollout_stage",
        "automation",
        "repository_grant",
        "github_app",
        "provider",
        "worker",
        "runtime",
    }
)


def _seg(value: str) -> str:
    """URL-quote one path segment — ids here are often LLM-authored, and an id
    containing '/' or '?' would otherwise reroute the request."""
    return quote(value, safe="")


def derive_changeset_idempotency_key(scope: str, *identity: str) -> str:
    """Derive a bounded canonical key from stable logical-effect identity."""
    if _IDEMPOTENCY_SCOPE_RE.fullmatch(scope) is None:
        raise ValueError("Changeset idempotency scope is not canonical")
    if not identity or any(not part for part in identity):
        raise ValueError("Changeset idempotency identity must not be empty")
    blob = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"agents:{scope}:{digest}"


async def _get(
    project_id: str, path: str, params: dict[str, Any] | None = None
) -> Any:
    async with httpx.AsyncClient(base_url=CODEGEN_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.get(path, params=params, headers=service_headers(project_id))
        resp.raise_for_status()
        return resp.json()


async def _post(
    project_id: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    async with httpx.AsyncClient(base_url=CODEGEN_SERVICE_URL, timeout=_TIMEOUT) as client:
        resp = await client.post(
            path, json=payload, headers=service_headers(project_id)
        )
        resp.raise_for_status()
        return resp.json()


async def open_changeset(
    project_id: str,
    title: str,
    spec: str,
    *,
    idempotency_key: str,
    run_id: str | None = None,
    base_branch: str | None = None,
    context: dict[str, Any] | None = None,
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    """Ask codegen to produce a changeset (branch + edits + draft PR) for a task.

    Returns the changeset record (includes ``changeset_id`` and ``status``).
    """
    if _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None:
        raise ValueError("idempotency_key must be a canonical 1 to 200 character key")

    payload: dict[str, Any] = {
        "project_id": project_id,
        "idempotency_key": idempotency_key,
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
    return await _post(project_id, "/v1/changesets", payload)


async def get_changeset_creation_capability(project_id: str) -> str:
    """Return Codegen's strict authenticated capability for one project."""
    payload = await _get(
        project_id,
        "/v1/capabilities/changeset-creation",
        params={"project_id": project_id},
    )
    if not isinstance(payload, dict) or set(payload) != {
        "project_id",
        "changeset_creation",
        "reasons",
        "checks",
    }:
        raise ValueError("Codegen capability response has an invalid schema")
    if payload["project_id"] != project_id:
        raise ValueError("Codegen capability response crossed project authority")
    state = payload["changeset_creation"]
    if state not in {"available", "disabled"}:
        raise ValueError("Codegen capability response has an invalid state")
    reasons = payload["reasons"]
    if (
        not isinstance(reasons, list)
        or len(reasons) != len(set(reasons))
        or any(reason not in _CAPABILITY_REASONS for reason in reasons)
    ):
        raise ValueError("Codegen capability response has invalid reasons")
    checks = payload["checks"]
    if (
        not isinstance(checks, dict)
        or set(checks) != _CAPABILITY_CHECKS
        or any(value not in {"ready", "blocked"} for value in checks.values())
    ):
        raise ValueError("Codegen capability response has invalid checks")
    if (state == "available") != (not reasons and all(v == "ready" for v in checks.values())):
        raise ValueError("Codegen capability response is internally inconsistent")
    return state


async def get_changeset(project_id: str, changeset_id: str) -> dict[str, Any]:
    """Fetch lifecycle, GitHub PR, external CI, and remediation projections."""
    return await _get(project_id, f"/v1/changesets/{_seg(changeset_id)}")


async def get_repo_context(project_id: str) -> dict[str, Any]:
    """Canonical repo profile (ecosystems, commands, contracts, CI, uncertainty).

    Grounds the feature-proposal prompt in what the repository actually is, so
    specs name real files and stay inside the repo's capabilities instead of
    demanding infrastructure it does not have.
    """
    return await _get(project_id, f"/v1/connections/{_seg(project_id)}/repo-context")


async def list_changesets(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """List the project's changesets (newest first), incl. task title + PR state."""
    return await _get(
        project_id,
        "/v1/changesets",
        params={"project_id": project_id, "limit": limit},
    )


async def revert_changeset(project_id: str, changeset_id: str) -> dict[str, Any]:
    """Roll back a merged changeset by opening a revert PR (a new changeset)."""
    return await _post(project_id, f"/v1/changesets/{_seg(changeset_id)}/revert")
