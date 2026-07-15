"""Enumerate the repositories the GitHub App can reach.

Walks every installation of the App (``GET /app/installations``, App JWT) and
each installation's repository grant (``GET /installation/repositories``,
installation token). The result powers the admin console's repo picker: connect
becomes "choose from what the App can already see" instead of hand-typing a
slug + installation id.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict

from app.config import github_api_url, github_app_id, github_app_private_key
from app.github.app_auth import build_app_jwt, mint_installation_token
from app.github.client import gh_client, gh_headers

_PER_PAGE = 100
#: Hard page cap per listing call — a runaway-pagination backstop far above any
#: realistic installation/repo count, not a silent truncation point.
_MAX_PAGES = 10


class AccessibleRepo(BaseModel):
    """One repository the App is installed on, as shown in the repo picker."""

    model_config = ConfigDict(extra="forbid")

    #: ``owner/name`` slug.
    repo: str
    installation_id: int
    #: The account (org/user) the installation lives under.
    account: str
    default_branch: str
    private: bool


async def _paginate(
    client: httpx.AsyncClient, url: str, token: str, *, items_key: str | None = None
) -> list[dict]:
    """Fetch every page of a GitHub list endpoint (plain array or keyed object)."""
    items: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        resp = await client.get(
            url,
            headers=gh_headers(token),
            params={"per_page": _PER_PAGE, "page": page},
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data[items_key] if items_key else data
        items.extend(batch)
        if len(batch) < _PER_PAGE:
            break
    return items


async def list_accessible_repos(
    *,
    app_id: str | None = None,
    private_key_pem: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[AccessibleRepo]:
    """Every repo the App can reach, across all installations.

    Args:
        app_id: Override the configured App ID (used in tests).
        private_key_pem: Override the configured private key (used in tests).
        client: Injectable httpx client (used in tests).

    Raises:
        ValueError: The App credentials are not configured (via
            :func:`build_app_jwt`).
    """
    resolved_app_id = app_id or github_app_id()
    resolved_key = private_key_pem or github_app_private_key()
    app_jwt = build_app_jwt(resolved_app_id, resolved_key)

    repos: list[AccessibleRepo] = []
    async with gh_client(client, timeout=30.0) as c:
        installations = await _paginate(
            c, f"{github_api_url()}/app/installations", app_jwt
        )
        for installation in installations:
            installation_id = int(installation["id"])
            account = (installation.get("account") or {}).get("login", "")
            token = await mint_installation_token(
                installation_id,
                app_id=resolved_app_id,
                private_key_pem=resolved_key,
                client=client,
            )
            granted = await _paginate(
                c,
                f"{github_api_url()}/installation/repositories",
                token.token,
                items_key="repositories",
            )
            repos.extend(
                AccessibleRepo(
                    repo=r["full_name"],
                    installation_id=installation_id,
                    account=account,
                    default_branch=r.get("default_branch") or "main",
                    private=bool(r.get("private", False)),
                )
                for r in granted
            )
    repos.sort(key=lambda r: r.repo.lower())
    return repos
