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
    node_id: str = ""


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    sha: str = ""


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

    return PullRequest(
        url=data["html_url"], number=data["number"], node_id=data.get("node_id", "")
    )


async def merge_pull_request(
    *,
    repo: str,
    number: int,
    token: str,
    merge_method: str = "squash",
    client: httpx.AsyncClient | None = None,
) -> MergeResult:
    """Merge a pull request (``owner/name``, PR number). GitHub enforces branch
    protection / required checks server-side as a backstop."""
    url = f"{github_api_url()}/repos/{repo}/pulls/{number}/merge"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.put(url, headers=headers, json={"merge_method": merge_method})
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()
    return MergeResult(merged=bool(data.get("merged")), sha=data.get("sha", ""))


async def mark_ready_for_review(
    *, node_id: str, token: str, client: httpx.AsyncClient | None = None
) -> None:
    """Promote a draft PR to ready-for-review (GraphQL) once CI is green (D5)."""
    if not node_id:
        return
    query = (
        "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id})"
        "{pullRequest{id isDraft}}}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.post(
            f"{github_api_url()}/graphql",
            headers=headers,
            json={"query": query, "variables": {"id": node_id}},
        )
        resp.raise_for_status()
    finally:
        if owns_client:
            await client.aclose()
