"""Read combined CI status for a git ref via the GitHub status + checks APIs."""

from __future__ import annotations

import logging

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers

logger = logging.getLogger(__name__)

_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


async def get_ci_status(
    repo: str, ref: str, token: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """Return ``"passed" | "failed" | "pending"`` for the latest checks on ``ref``.

    Combines the legacy commit-status rollup with the Checks API check-runs: any
    failure → ``failed``; anything still running → ``pending``; all green with at
    least one signal → ``passed``; no signal at all → ``pending`` (logged, since
    a repo with no checks configured otherwise just looks stuck in ci_running).
    """
    base = github_api_url()
    async with gh_client(client) as c:
        status_resp = await c.get(
            f"{base}/repos/{repo}/commits/{ref}/status", headers=gh_headers(token)
        )
        status_resp.raise_for_status()
        combined = status_resp.json()
        runs_resp = await c.get(
            f"{base}/repos/{repo}/commits/{ref}/check-runs", headers=gh_headers(token)
        )
        runs_resp.raise_for_status()
        check_runs = runs_resp.json().get("check_runs", [])

    state = combined.get("state", "")
    total = combined.get("total_count", 0)

    if state in ("failure", "error"):
        return "failed"
    for run in check_runs:
        if run.get("status") != "completed":
            return "pending"
        if run.get("conclusion") in _FAIL_CONCLUSIONS:
            return "failed"
    if state == "pending" and total > 0:
        return "pending"
    if total > 0 or check_runs:
        return "passed"

    # No commit statuses and no check-runs at all: we cannot verify green, so we
    # deliberately hold at "pending" (don't merge what we can't check). Log it —
    # to an operator the changeset just looks stuck in ci_running, and a repo
    # with no CI configured will never auto-advance.
    logger.info(
        "No CI signal for %s@%s (no commit statuses, no check-runs); holding as "
        "pending. If this repo has no checks, the changeset will not auto-advance.",
        repo,
        ref,
    )
    return "pending"
