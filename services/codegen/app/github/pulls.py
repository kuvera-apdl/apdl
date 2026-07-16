"""Strict, recoverable GitHub pull-request publication helpers.

The raw accepted response is durably recorded through ``on_accepted`` before
the response is interpreted as an APDL-owned pull request.  Recovery searches
the deterministic APDL branch before issuing another create request.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import github_api_url
from app.github.client import gh_client, gh_headers, github_next_page
from app.models.observations import GitHubPRStatus
from app.models.pr_publication import PullRequestAcceptedReceipt


AcceptanceRecorder = Callable[[PullRequestAcceptedReceipt], Awaitable[None]]
_POSTGRES_INTEGER_MAX = 2_147_483_647
_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True)
class PullRequest:
    """Strict GitHub identity validated against the publication intent."""

    repository: str
    repository_id: int
    url: str
    number: int
    head_ref: str
    base_ref: str
    head_sha: str
    status: GitHubPRStatus
    github_updated_at: datetime


class PullRequestIdentityError(ValueError):
    """GitHub accepted or returned a PR that does not match APDL's intent."""

    def __init__(self, message: str, receipt: PullRequestAcceptedReceipt) -> None:
        super().__init__(message)
        self.receipt = receipt


class PullRequestDiscoveryError(RuntimeError):
    """The branch recovery lookup could not produce one unambiguous identity."""

    def __init__(
        self,
        message: str,
        receipts: tuple[PullRequestAcceptedReceipt, ...] = (),
    ) -> None:
        super().__init__(message)
        self.receipts = receipts


def _positive_pr_number(value: Any) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _POSTGRES_INTEGER_MAX
    ):
        return None
    return value


def _positive_github_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _absolute_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _url_matches_pull_request(url: str, repository: str, number: int) -> bool:
    parsed = urlparse(url)
    api = urlparse(github_api_url())
    expected_netloc = "github.com" if api.netloc == "api.github.com" else api.netloc
    return (
        parsed.scheme == api.scheme
        and parsed.netloc == expected_netloc
        and parsed.path == f"/{repository}/pull/{number}"
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _receipt(
    *,
    source: str,
    repository: str,
    head: str,
    base: str,
    status_code: int,
    raw_response: Any,
) -> PullRequestAcceptedReceipt:
    payload = raw_response if isinstance(raw_response, dict) else {}
    pr_number = _positive_pr_number(payload.get("number"))
    github_url = _absolute_url(payload.get("html_url"))
    if (
        pr_number is None
        or github_url is None
        or not _url_matches_pull_request(github_url, repository, pr_number)
    ):
        github_url = None
    return PullRequestAcceptedReceipt(
        source=source,
        repository=repository,
        requested_head=head,
        requested_base=base,
        accepted_at=datetime.now(timezone.utc),
        status_code=status_code,
        pr_number=pr_number,
        github_url=github_url,
        raw_response=raw_response,
    )


async def _record_accepted(
    recorder: AcceptanceRecorder,
    receipt: PullRequestAcceptedReceipt,
) -> None:
    """Finish the durable write even if request cancellation arrives."""
    task = asyncio.create_task(recorder(receipt))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    task.result()
    if cancellation is not None:
        raise cancellation


def validate_pull_request(
    pull_request: PullRequest,
    *,
    repository: str,
    repository_id: int,
    head: str,
    base: str,
    expected_head_sha: str,
    require_live: bool = True,
) -> PullRequest:
    """Reject a typed result that differs from any immutable intent field."""
    if pull_request.repository != repository:
        raise ValueError("GitHub pull request belongs to a different repository")
    if pull_request.repository_id != repository_id:
        raise ValueError("GitHub pull request has a different repository identity")
    if not _url_matches_pull_request(pull_request.url, repository, pull_request.number):
        raise ValueError("GitHub pull request URL has a different identity")
    if pull_request.head_ref != head:
        raise ValueError("GitHub pull request has a different head branch")
    if pull_request.base_ref != base:
        raise ValueError("GitHub pull request has a different base branch")
    if pull_request.head_sha != expected_head_sha:
        raise ValueError("GitHub pull request has a different exact head SHA")
    if require_live and pull_request.status not in {
        GitHubPRStatus.draft,
        GitHubPRStatus.open,
    }:
        raise ValueError("GitHub pull request is not externally live")
    return pull_request


def _validate_payload(
    payload: Any,
    *,
    receipt: PullRequestAcceptedReceipt,
    repository: str,
    repository_id: int,
    head: str,
    base: str,
    expected_head_sha: str,
    require_live: bool,
    require_exact_head: bool = True,
) -> PullRequest:
    def invalid(message: str) -> PullRequestIdentityError:
        return PullRequestIdentityError(message, receipt)

    if not isinstance(payload, dict):
        raise invalid("GitHub pull-request response must be an object")
    number = _positive_pr_number(payload.get("number"))
    if number is None:
        raise invalid("GitHub pull-request response has no positive number")
    url = _absolute_url(payload.get("html_url"))
    if url is None:
        raise invalid("GitHub pull-request response has no absolute URL")
    if not _url_matches_pull_request(url, repository, number):
        raise invalid("GitHub pull-request response URL has a different identity")
    head_payload = payload.get("head")
    base_payload = payload.get("base")
    if not isinstance(head_payload, dict) or not isinstance(base_payload, dict):
        raise invalid("GitHub pull-request response has no head/base identity")
    head_repo = head_payload.get("repo")
    base_repo = base_payload.get("repo")
    if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
        raise invalid("GitHub pull-request response has no repository identity")
    if (
        _positive_github_id(head_repo.get("id")) != repository_id
        or _positive_github_id(base_repo.get("id")) != repository_id
    ):
        raise invalid("GitHub pull-request response has a repository mismatch")
    head_ref = head_payload.get("ref")
    base_ref = base_payload.get("ref")
    head_sha = head_payload.get("sha")
    if head_ref != head or base_ref != base:
        raise invalid("GitHub pull-request response has a branch mismatch")
    if not isinstance(head_sha, str) or _SHA_PATTERN.fullmatch(head_sha) is None:
        raise invalid("GitHub pull-request response has an invalid head SHA")
    if require_exact_head and head_sha != expected_head_sha:
        raise invalid("GitHub pull-request response has an exact-head mismatch")
    draft = payload.get("draft")
    state = payload.get("state")
    if state == "open":
        if not isinstance(draft, bool):
            raise invalid("GitHub open pull-request response has no draft state")
        status = GitHubPRStatus.draft if draft else GitHubPRStatus.open
    elif state == "closed":
        merged_at = payload.get("merged_at")
        if merged_at is not None and not isinstance(merged_at, str):
            raise invalid("GitHub closed pull-request response has invalid merged_at")
        status = GitHubPRStatus.merged if merged_at else GitHubPRStatus.closed
    else:
        raise invalid("GitHub pull-request response has an unsupported state")
    if require_live and status not in {GitHubPRStatus.draft, GitHubPRStatus.open}:
        raise invalid("GitHub pull-request response is not externally live")
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise invalid("GitHub pull-request response is missing updated_at")
    try:
        github_updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise invalid("GitHub pull-request response has invalid updated_at") from exc
    if github_updated_at.tzinfo is None or github_updated_at.utcoffset() is None:
        raise invalid("GitHub pull-request updated_at must include a timezone")
    return PullRequest(
        repository=repository,
        repository_id=repository_id,
        url=url,
        number=number,
        head_ref=head,
        base_ref=base,
        head_sha=head_sha,
        status=status,
        github_updated_at=github_updated_at,
    )


async def open_pull_request(
    *,
    repo: str,
    repository_id: int,
    head: str,
    base: str,
    expected_head_sha: str,
    title: str,
    body: str,
    token: str,
    on_accepted: AcceptanceRecorder,
    draft: bool = True,
    client: httpx.AsyncClient | None = None,
) -> PullRequest:
    """Create a PR, journal the accepted response, then validate its identity."""
    url = f"{github_api_url()}/repos/{repo}/pulls"
    payload = {"title": title, "head": head, "base": base, "body": body, "draft": draft}

    async with gh_client(client) as c:
        response = await c.post(url, headers=gh_headers(token), json=payload)
        response.raise_for_status()
        try:
            raw_response: Any = response.json()
        except ValueError:
            raw_response = response.text
        receipt = _receipt(
            source="create",
            repository=repo,
            head=head,
            base=base,
            status_code=response.status_code,
            raw_response=raw_response,
        )
        await _record_accepted(on_accepted, receipt)
    return _validate_payload(
        raw_response,
        receipt=receipt,
        repository=repo,
        repository_id=repository_id,
        head=head,
        base=base,
        expected_head_sha=expected_head_sha,
        require_live=True,
    )


async def find_pull_request_by_branch(
    *,
    repo: str,
    repository_id: int,
    head: str,
    base: str,
    expected_head_sha: str,
    token: str,
    on_accepted: AcceptanceRecorder,
    client: httpx.AsyncClient | None = None,
) -> PullRequest | None:
    """Recover one PR in any state by the deterministic APDL branch."""
    owner = repo.partition("/")[0]
    url = f"{github_api_url()}/repos/{repo}/pulls"
    params = {
        "state": "all",
        "head": f"{owner}:{head}",
        "base": base,
        "per_page": "100",
    }
    async with gh_client(client) as c:
        response = await c.get(url, headers=gh_headers(token), params=params)
        response.raise_for_status()
        raw_response = response.json()
        if not isinstance(raw_response, list):
            raise PullRequestDiscoveryError(
                "GitHub branch recovery response must be an array"
            )
        next_page = github_next_page(
            response,
            error_type=PullRequestDiscoveryError,
        )
        if not raw_response:
            if next_page is not None:
                raise PullRequestDiscoveryError(
                    "GitHub branch recovery pagination is incomplete"
                )
            return None
        receipts = tuple(
            _receipt(
                source="recovery",
                repository=repo,
                head=head,
                base=base,
                status_code=response.status_code,
                raw_response=payload,
            )
            for payload in raw_response
        )
        for receipt in receipts:
            await _record_accepted(on_accepted, receipt)
        if next_page is not None:
            raise PullRequestDiscoveryError(
                "GitHub branch recovery pagination is incomplete",
                receipts,
            )

        validated: list[PullRequest] = []
        identity_errors: list[PullRequestIdentityError] = []
        for payload, receipt in zip(raw_response, receipts, strict=True):
            try:
                validated.append(
                    _validate_payload(
                        payload,
                        receipt=receipt,
                        repository=repo,
                        repository_id=repository_id,
                        head=head,
                        base=base,
                        expected_head_sha=expected_head_sha,
                        require_live=False,
                    )
                )
            except PullRequestIdentityError as exc:
                identity_errors.append(exc)
        if identity_errors:
            if len(raw_response) == 1:
                raise identity_errors[0]
            raise PullRequestDiscoveryError(
                "GitHub branch recovery returned invalid or ambiguous identities",
                receipts,
            )
        live = [
            pull_request
            for pull_request in validated
            if pull_request.status in {GitHubPRStatus.draft, GitHubPRStatus.open}
        ]
        if len(live) == 1:
            return live[0]
        if len(live) > 1 or len(validated) > 1:
            raise PullRequestDiscoveryError(
                "GitHub branch recovery returned multiple pull requests",
                receipts,
            )
        return validated[0]


async def close_pull_request(
    *,
    repo: str,
    repository_id: int,
    number: int,
    head: str,
    base: str,
    expected_head_sha: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Validate APDL ownership, then close the exact unmerged pull request."""
    url = f"{github_api_url()}/repos/{repo}/pulls/{number}"
    async with gh_client(client) as c:
        response = await c.get(url, headers=gh_headers(token))
        response.raise_for_status()
        payload = response.json()
        receipt = _receipt(
            source="recovery",
            repository=repo,
            head=head,
            base=base,
            status_code=response.status_code,
            raw_response=payload,
        )
        pull_request = _validate_payload(
            payload,
            receipt=receipt,
            repository=repo,
            repository_id=repository_id,
            head=head,
            base=base,
            expected_head_sha=expected_head_sha,
            require_live=False,
            require_exact_head=False,
        )
        if pull_request.number != number:
            raise PullRequestIdentityError(
                "GitHub cleanup lookup returned a different pull-request number",
                receipt,
            )
        if pull_request.status is GitHubPRStatus.merged:
            raise PullRequestIdentityError(
                "GitHub cleanup target is already merged and cannot be closed",
                receipt,
            )
        if pull_request.status is GitHubPRStatus.closed:
            return
        response = await c.patch(
            url,
            headers=gh_headers(token),
            json={"state": "closed"},
        )
        response.raise_for_status()
        payload = response.json()
        receipt = _receipt(
            source="recovery",
            repository=repo,
            head=head,
            base=base,
            status_code=response.status_code,
            raw_response=payload,
        )
        confirmed = _validate_payload(
            payload,
            receipt=receipt,
            repository=repo,
            repository_id=repository_id,
            head=head,
            base=base,
            expected_head_sha=expected_head_sha,
            require_live=False,
            require_exact_head=False,
        )
        if confirmed.number != number or confirmed.status is not GitHubPRStatus.closed:
            raise PullRequestIdentityError(
                "GitHub did not confirm the exact pull request was closed",
                receipt,
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
        response = await c.get(url, headers=gh_headers(token))
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("GitHub pull-request response must be an object")
    return data
