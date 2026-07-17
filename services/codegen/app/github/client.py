"""Shared httpx plumbing for the GitHub App REST/GraphQL calls.

``app_auth``, ``checks``, and ``pulls`` all talk to GitHub the same way: a bearer
token, the ``application/vnd.github+json`` accept header + pinned API version,
and an httpx client that is either injected by the caller (so tests can supply a
``MockTransport``) or created and closed by us. This module owns that one header
shape + client lifecycle so the call sites stop re-implementing both.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import github_api_url

_TIMEOUT = 30.0


class GitHubPaginationIncompleteError(ValueError):
    """Raised when a bounded GitHub pagination walk has unread pages."""


def _validate_github_api_url(
    url: str,
    *,
    error_type: type[ValueError],
) -> str:
    target = httpx.URL(url)
    configured = httpx.URL(github_api_url())
    if target.userinfo or (
        target.scheme,
        target.host,
        target.port,
    ) != (
        configured.scheme,
        configured.host,
        configured.port,
    ):
        raise error_type("GitHub request attempted to leave the configured API host")
    return url


def gh_headers(token: str) -> dict[str, str]:
    """Standard GitHub App auth headers for a bearer ``token`` (JWT or install token)."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@contextlib.asynccontextmanager
async def gh_client(
    client: httpx.AsyncClient | None, *, timeout: float = _TIMEOUT
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an httpx client, closing it only if we created it.

    Pass an existing ``client`` (e.g. a test ``MockTransport``) to reuse it; pass
    ``None`` to get a short-lived client that is closed on exit.
    """
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        yield client
    finally:
        if owns:
            await client.aclose()


def github_next_page(
    response: httpx.Response,
    *,
    error_type: type[ValueError] = ValueError,
) -> str | None:
    """Return a same-origin GitHub ``rel=next`` URL, if one exists.

    Pagination links are controlled by the remote response.  Keeping the
    origin check in the shared client layer prevents every caller from growing
    a subtly different token-forwarding boundary.
    """
    next_url = (response.links.get("next") or {}).get("url")
    if next_url is None:
        return None
    return _validate_github_api_url(next_url, error_type=error_type)


async def github_json_pages(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    max_pages: int,
    error_type: type[ValueError] = ValueError,
) -> AsyncIterator[dict[str, Any]]:
    """Yield one complete bounded same-origin walk of object-shaped JSON pages.

    A page cap is a resource bound, not evidence that the collection is
    complete. If GitHub still advertises ``rel=next`` after the final permitted
    page, fail closed instead of silently returning a truncated collection.
    """
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    next_url: str | None = _validate_github_api_url(url, error_type=error_type)
    for _ in range(max_pages):
        if next_url is None:
            break
        response = await client.get(next_url, headers=gh_headers(token))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise error_type("GitHub paginated response must be an object")
        yield payload
        next_url = github_next_page(response, error_type=error_type)
    if next_url is not None:
        message = (
            f"GitHub pagination exceeded max_pages={max_pages} "
            "while another page remained"
        )
        if error_type is ValueError:
            raise GitHubPaginationIncompleteError(message)
        raise error_type(message)


async def github_paginated_items(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    key: str,
    *,
    max_pages: int,
    error_type: type[ValueError] = ValueError,
) -> list[Any]:
    """Collect one list-valued field across bounded GitHub JSON pages."""
    items: list[Any] = []
    async for payload in github_json_pages(
        client,
        url,
        token,
        max_pages=max_pages,
        error_type=error_type,
    ):
        page_items = payload.get(key, [])
        if not isinstance(page_items, list):
            raise error_type(f"GitHub paginated response field {key!r} must be a list")
        items.extend(page_items)
    return items
