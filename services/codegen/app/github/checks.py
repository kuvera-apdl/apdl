"""Read bounded raw GitHub CI evidence for one exact commit head."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.config import github_api_url
from app.github.client import (
    GitHubPaginationIncompleteError,
    gh_client,
    gh_headers,
    github_json_pages,
)

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
    complete: bool = True
    incomplete_reason: str | None = None

    def __post_init__(self) -> None:
        if self.complete:
            if self.incomplete_reason is not None:
                raise ValueError("complete CI evidence cannot have an incomplete reason")
            return
        if not self.incomplete_reason:
            raise ValueError("incomplete CI evidence requires a reason")
        if self.check_runs or self.combined_status.get("statuses"):
            raise ValueError("incomplete CI evidence cannot expose partial signals")

    @classmethod
    def incomplete(cls, head_sha: str, reason: str) -> GitHubCIEvidence:
        """Return explicit fail-closed evidence with no authoritative signals."""
        return cls(
            combined_status={"sha": head_sha, "statuses": []},
            check_runs=[],
            complete=False,
            incomplete_reason=reason,
        )


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
        status_task = asyncio.create_task(
            _get_combined_status(c, repo, head_sha, token)
        )
        checks_task = asyncio.create_task(
            _get_check_runs(
                c,
                repo,
                head_sha,
                token,
            )
        )
        try:
            combined, raw_check_runs = await asyncio.gather(
                status_task, checks_task
            )
        except GitHubPaginationIncompleteError as exc:
            for task in (status_task, checks_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(status_task, checks_task, return_exceptions=True)
            logger.warning(
                "GitHub returned incomplete CI evidence for %s at %s: %s",
                repo,
                head_sha,
                exc,
            )
            return GitHubCIEvidence.incomplete(head_sha, str(exc))
        except BaseException:
            # asyncio.gather propagates the first exception but deliberately
            # leaves siblings running. Cancel and join both requests before the
            # shared client context can close underneath the surviving task.
            for task in (status_task, checks_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(status_task, checks_task, return_exceptions=True)
            raise
        if not all(isinstance(run, dict) for run in raw_check_runs):
            raise ValueError("GitHub check-run entries must be objects")
        check_runs: list[dict] = raw_check_runs
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


async def _get_combined_status(
    client: httpx.AsyncClient,
    repo: str,
    head_sha: str,
    token: str,
) -> dict:
    url = (
        f"{github_api_url()}/repos/{repo}/commits/{head_sha}/status"
        f"?per_page={_PER_PAGE}"
    )
    combined: dict | None = None
    statuses: list[dict] = []
    expected_total: int | None = None
    expected_identity: tuple[object, object] | None = None
    async for payload in github_json_pages(
        client,
        url,
        token,
        max_pages=_MAX_PAGES,
    ):
        page_statuses = payload.get("statuses", [])
        if not isinstance(page_statuses, list):
            raise ValueError("GitHub combined statuses must be a list")
        if not all(isinstance(status, dict) for status in page_statuses):
            raise ValueError("GitHub commit-status entries must be objects")
        page_total = _total_count(payload, "combined status")
        identity = (payload.get("sha"), payload.get("state"))
        if combined is None:
            combined = dict(payload)
            expected_total = page_total
            expected_identity = identity
        elif page_total != expected_total or identity != expected_identity:
            raise GitHubPaginationIncompleteError(
                "GitHub combined status changed during pagination"
            )
        statuses.extend(page_statuses)
    if combined is None:
        raise ValueError("GitHub combined status must be an object")
    _assert_complete_count("combined statuses", statuses, expected_total)
    combined["statuses"] = statuses
    return combined


async def _get_check_runs(
    client: httpx.AsyncClient,
    repo: str,
    head_sha: str,
    token: str,
) -> list[dict]:
    url = (
        f"{github_api_url()}/repos/{repo}/commits/{head_sha}/check-runs"
        f"?per_page={_PER_PAGE}"
    )
    check_runs: list[dict] = []
    expected_total: int | None = None
    first_page = True
    async for payload in github_json_pages(
        client,
        url,
        token,
        max_pages=_MAX_PAGES,
    ):
        page_runs = payload.get("check_runs", [])
        if not isinstance(page_runs, list):
            raise ValueError("GitHub check-runs response must contain a list")
        if not all(isinstance(run, dict) for run in page_runs):
            raise ValueError("GitHub check-run entries must be objects")
        page_total = _total_count(payload, "check runs")
        if first_page:
            expected_total = page_total
            first_page = False
        elif page_total != expected_total:
            raise GitHubPaginationIncompleteError(
                "GitHub check-run total changed during pagination"
            )
        check_runs.extend(page_runs)
    _assert_complete_count("check runs", check_runs, expected_total)
    return check_runs


def _total_count(payload: dict, source: str) -> int | None:
    if "total_count" not in payload:
        return None
    value = payload["total_count"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"GitHub {source} total_count must be a non-negative integer")
    return value


def _assert_complete_count(
    source: str,
    items: list[dict],
    expected_total: int | None,
) -> None:
    if expected_total is not None and len(items) != expected_total:
        raise GitHubPaginationIncompleteError(
            f"GitHub {source} reported {expected_total} items "
            f"but returned {len(items)}"
        )
