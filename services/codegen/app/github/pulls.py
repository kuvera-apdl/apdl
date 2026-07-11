"""Open pull requests via the GitHub App installation token.

codegen opens the PR itself (rather than letting the editing agent do it).
GitHub owns verification and merge.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers

@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int
    node_id: str = ""


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

    return PullRequest(
        url=data["html_url"], number=data["number"], node_id=data.get("node_id", "")
    )


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
    async with gh_client(client) as c:
        resp = await c.post(
            f"{github_api_url()}/graphql",
            headers=gh_headers(token),
            json={"query": query, "variables": {"id": node_id}},
        )
        resp.raise_for_status()


async def close_pull_request(
    *,
    repo: str,
    number: int,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Close a pull request without merging (``PATCH`` state=closed).

    Used by ``/abandon``: the change is being dropped, not landed. GitHub returns
    200 even if the PR is already closed, so this is safe to call idempotently.
    The head branch is intentionally left in place (it can be reopened/inspected).
    """
    url = f"{github_api_url()}/repos/{repo}/pulls/{number}"
    async with gh_client(client) as c:
        resp = await c.patch(url, headers=gh_headers(token), json={"state": "closed"})
        resp.raise_for_status()
