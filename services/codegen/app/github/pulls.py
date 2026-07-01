"""Open pull requests via the GitHub App installation token.

codegen opens the PR itself (rather than letting the editing agent do it) so the
merge decision stays in APDL's gated path. PRs open as drafts (plan decision
D5); a later phase promotes them to ready-for-review once CI is green.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers

#: GitHub status codes on the merge endpoint that mean "this PR can't be merged
#: right now" rather than a server fault: 405 not mergeable, 409 head moved / SHA
#: mismatch, 422 required checks unmet. These are surfaced as a clean not-merged
#: result so the caller can return a 409, not an unhandled 500.
_NOT_MERGEABLE = frozenset({405, 409, 422})


@dataclass(frozen=True)
class PullRequest:
    url: str
    number: int
    node_id: str = ""


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    sha: str = ""
    reason: str = ""  # why GitHub declined, when ``merged`` is False


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


async def merge_pull_request(
    *,
    repo: str,
    number: int,
    token: str,
    merge_method: str = "squash",
    client: httpx.AsyncClient | None = None,
) -> MergeResult:
    """Merge a pull request (``owner/name``, PR number). GitHub enforces branch
    protection / required checks server-side as a backstop.

    A GitHub *refusal* to merge (405 not mergeable / 409 head moved / 422 checks
    unmet) is returned as ``MergeResult(merged=False, reason=...)`` rather than
    raised, so the caller maps it to a clean 409. Other transport/5xx errors
    still propagate.
    """
    url = f"{github_api_url()}/repos/{repo}/pulls/{number}/merge"
    async with gh_client(client) as c:
        resp = await c.put(
            url, headers=gh_headers(token), json={"merge_method": merge_method}
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _NOT_MERGEABLE:
                return MergeResult(merged=False, reason=_merge_refusal_reason(exc.response))
            raise
        data = resp.json()
    merged = bool(data.get("merged"))
    return MergeResult(
        merged=merged,
        sha=data.get("sha", ""),
        reason="" if merged else (data.get("message") or "GitHub declined the merge."),
    )


def _merge_refusal_reason(response: httpx.Response) -> str:
    """Pull GitHub's human message off a not-mergeable response (best effort)."""
    try:
        return response.json().get("message") or "GitHub declined the merge."
    except ValueError:
        return "GitHub declined the merge."


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
