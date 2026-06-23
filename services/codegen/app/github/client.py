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

import httpx

_TIMEOUT = 30.0


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
