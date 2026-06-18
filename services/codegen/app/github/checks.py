"""Read combined CI status for a git ref via the GitHub status + checks APIs."""

from __future__ import annotations

import httpx

from app.config import github_api_url

_TIMEOUT = 30.0
_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def get_ci_status(
    repo: str, ref: str, token: str, *, client: httpx.AsyncClient | None = None
) -> str:
    """Return ``"passed" | "failed" | "pending"`` for the latest checks on ``ref``.

    Combines the legacy commit-status rollup with the Checks API check-runs: any
    failure → ``failed``; anything still running → ``pending``; all green with at
    least one signal → ``passed``; no signal at all → ``pending``.
    """
    base = github_api_url()
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        status_resp = await client.get(
            f"{base}/repos/{repo}/commits/{ref}/status", headers=_headers(token)
        )
        status_resp.raise_for_status()
        combined = status_resp.json()
        runs_resp = await client.get(
            f"{base}/repos/{repo}/commits/{ref}/check-runs", headers=_headers(token)
        )
        runs_resp.raise_for_status()
        check_runs = runs_resp.json().get("check_runs", [])
    finally:
        if owns_client:
            await client.aclose()

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
    return "pending"
