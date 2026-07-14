"""Open pull requests via the GitHub App installation token.

codegen opens the PR itself (rather than letting the editing agent do it).
GitHub owns verification and merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers
from app.models.observations import GitHubPRStatus

@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int
    head_sha: str
    status: GitHubPRStatus
    github_updated_at: datetime


async def open_pull_request(
    *,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    token: str,
    draft: bool = True,
    client: httpx.AsyncClient | None = None,
) -> PullRequest:
    """Open a pull request on ``repo`` (``owner/name``) and return its URL + number."""
    url = f"{github_api_url()}/repos/{repo}/pulls"
    payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}

    async with gh_client(client) as c:
        resp = await c.post(url, headers=gh_headers(token), json=payload)
        resp.raise_for_status()
        data = resp.json()

    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ValueError("GitHub pull-request response is missing updated_at")
    try:
        github_updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("GitHub pull-request response has invalid updated_at") from exc
    if github_updated_at.tzinfo is None or github_updated_at.utcoffset() is None:
        raise ValueError("GitHub pull-request updated_at must include a timezone")
    return PullRequest(
        url=data["html_url"],
        number=data["number"],
        head_sha=str((data.get("head") or {}).get("sha") or ""),
        status=(
            GitHubPRStatus.draft
            if data.get("draft") is True
            else GitHubPRStatus.open
        ),
        github_updated_at=github_updated_at,
    )


async def get_pull_request(
    repo: str,
    number: int,
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Read the live GitHub PR used by webhook and polling recovery."""
    url = f"{github_api_url()}/repos/{repo}/pulls/{number}"
    async with gh_client(client) as c:
        resp = await c.get(url, headers=gh_headers(token))
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("GitHub pull-request response must be an object")
    return data
