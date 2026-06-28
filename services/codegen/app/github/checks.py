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
    """Return ``"passed" | "failed" | "pending" | "none"`` for ``ref``'s checks.

    Combines the legacy commit-status rollup with the Checks API check-runs: any
    failure → ``failed``; anything still running → ``pending``; all green with at
    least one signal → ``passed``.

    When ``ref`` has no signal at all, the repo's Actions workflows break the tie:
    a repo with **zero** workflows has no CI to wait on → ``none`` (callers may
    treat this as "nothing blocks a merge", rather than holding forever); a repo
    that *has* workflows but hasn't reported yet is still ``pending`` (its checks
    are most likely queued and about to start, e.g. right after the PR opened).
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

    # No commit statuses and no check-runs on this ref. Ask whether the repo has
    # any Actions workflows at all to tell "no CI configured" apart from "CI is
    # configured but hasn't reported yet" — otherwise a no-CI repo sits in
    # ci_running forever and blocks merges nothing will ever green-light.
    return await _no_signal_status(base, repo, ref, token, client=client)


async def _no_signal_status(
    base: str, repo: str, ref: str, token: str, *, client: httpx.AsyncClient | None
) -> str:
    """Resolve the no-signal case: ``none`` if the repo has no Actions workflows,
    else ``pending`` (checks are likely queued and about to start)."""
    async with gh_client(client) as c:
        wf_resp = await c.get(
            f"{base}/repos/{repo}/actions/workflows", headers=gh_headers(token)
        )
        wf_resp.raise_for_status()
        workflow_count = wf_resp.json().get("total_count", 0)

    if workflow_count > 0:
        logger.info(
            "No CI signal yet for %s@%s but %d workflow(s) exist; holding as pending.",
            repo,
            ref,
            workflow_count,
        )
        return "pending"

    logger.info(
        "No CI configured for %s@%s (0 workflows, no statuses/checks); reporting "
        "'none' so the changeset is not blocked on CI that will never run.",
        repo,
        ref,
    )
    return "none"
