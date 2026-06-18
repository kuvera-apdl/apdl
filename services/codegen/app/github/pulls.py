"""Open pull requests via the GitHub App installation token.

codegen opens the PR itself (rather than letting the editing agent do it) so the
merge decision stays in APDL's gated path. PRs open as drafts (plan decision
D5); a later phase promotes them to ready-for-review once CI is green.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import github_api_url

_TIMEOUT = 30.0


@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int


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
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    return PullRequest(url=data["html_url"], number=data["number"])
