"""Read bounded raw GitHub CI evidence for one exact commit head."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers

logger = logging.getLogger(__name__)

_FAIL_CONCLUSIONS = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
}

#: Page size + page cap for the paginated Checks-API reads. GitHub defaults to
#: 30 per page; a repo with more check runs than one page would otherwise have
#: its later runs (possibly the failing ones) silently ignored. The cap bounds
#: a pathological repo at 1000 runs — far beyond anything a merge gate needs.
_PER_PAGE = 100
_MAX_PAGES = 10
_MAX_FAILED_RUNS_WITH_ANNOTATIONS = 10
_MAX_ANNOTATIONS_PER_RUN = 50


@dataclass(frozen=True)
class GitHubCIEvidence:
    combined_status: dict
    check_runs: list[dict]


async def get_ci_evidence(
    repo: str,
    head_sha: str,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> GitHubCIEvidence:
    """Fetch bounded raw status/check evidence for one exact GitHub head."""
    base = github_api_url()
    async with gh_client(client) as c:
        status_resp = await c.get(
            f"{base}/repos/{repo}/commits/{head_sha}/status",
            headers=gh_headers(token),
        )
        status_resp.raise_for_status()
        combined = status_resp.json()
        if not isinstance(combined, dict):
            raise ValueError("GitHub combined status must be an object")
        check_runs = await _paginated_list(
            c,
            f"{base}/repos/{repo}/commits/{head_sha}/check-runs",
            token,
            "check_runs",
        )
        failed_runs = [
            run
            for run in check_runs
            if run.get("status") == "completed"
            and run.get("conclusion") in _FAIL_CONCLUSIONS
            and run.get("id")
        ][:_MAX_FAILED_RUNS_WITH_ANNOTATIONS]
        for run in failed_runs:
            try:
                annotations_resp = await c.get(
                    f"{base}/repos/{repo}/check-runs/{run['id']}/annotations",
                    headers=gh_headers(token),
                    params={"per_page": _MAX_ANNOTATIONS_PER_RUN},
                )
                annotations_resp.raise_for_status()
                annotations = annotations_resp.json()
                if isinstance(annotations, list):
                    run["_failure_annotations"] = annotations[:_MAX_ANNOTATIONS_PER_RUN]
            except httpx.HTTPError:
                logger.warning(
                    "Could not read failure annotations for %s check run %s.",
                    repo,
                    run["id"],
                )
    return GitHubCIEvidence(combined_status=combined, check_runs=check_runs)


async def _paginated_list(
    c: httpx.AsyncClient, url: str, token: str, key: str
) -> list[dict]:
    """Collect ``key`` items across GitHub Link-header pages (bounded)."""
    items: list[dict] = []
    next_url: str | None = f"{url}?per_page={_PER_PAGE}"
    for _ in range(_MAX_PAGES):
        if next_url is None:
            break
        resp = await c.get(next_url, headers=gh_headers(token))
        resp.raise_for_status()
        items.extend(resp.json().get(key) or [])
        next_url = (resp.links.get("next") or {}).get("url")
    return items
