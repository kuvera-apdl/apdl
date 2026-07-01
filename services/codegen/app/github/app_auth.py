"""GitHub App authentication — mint short-lived installation access tokens.

The codegen service authenticates to customer repositories as a GitHub App: it
signs a short-lived JWT with the App private key, then exchanges it for a
per-installation access token (<=1h TTL) scoped to that customer's repos. No
long-lived PAT is ever stored, and installation tokens are minted per job and
never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import jwt

from app.config import github_api_url, github_app_id, github_app_private_key
from app.github.client import gh_client, gh_headers

#: GitHub rejects App JWTs whose ``exp`` is more than 10 minutes out. Use a
#: conservative 9-minute window and backdate ``iat`` by 60s for clock skew.
_JWT_TTL = timedelta(minutes=9)
_CLOCK_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class InstallationToken:
    """A minted installation access token and its expiry (UTC)."""

    token: str
    expires_at: datetime


def build_app_jwt(app_id: str, private_key_pem: str, *, now: datetime | None = None) -> str:
    """Build a signed App JWT (RS256) for authenticating as the GitHub App.

    Args:
        app_id: The GitHub App's numeric ID (as a string).
        private_key_pem: The App's PEM-encoded RSA private key.
        now: Reference time (UTC); defaults to the current time. Injectable for
            deterministic testing.

    Returns:
        A serialized RS256 JWT.
    """
    if not app_id or not private_key_pem:
        raise ValueError("GitHub App ID and private key are required to mint a JWT.")
    moment = now or datetime.now(timezone.utc)
    payload = {
        "iat": int((moment - _CLOCK_SKEW).timestamp()),
        "exp": int((moment + _JWT_TTL).timestamp()),
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


async def mint_installation_token(
    installation_id: int,
    *,
    app_id: str | None = None,
    private_key_pem: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> InstallationToken:
    """Exchange an App JWT for an installation access token.

    Args:
        installation_id: The target installation (one per customer org/repo set).
        app_id: Override the configured App ID (used in tests).
        private_key_pem: Override the configured private key (used in tests).
        client: Injectable httpx client (used in tests).

    Returns:
        An :class:`InstallationToken`.
    """
    resolved_app_id = app_id or github_app_id()
    resolved_key = private_key_pem or github_app_private_key()
    app_jwt = build_app_jwt(resolved_app_id, resolved_key)

    url = f"{github_api_url()}/app/installations/{installation_id}/access_tokens"
    async with gh_client(client, timeout=15.0) as c:
        resp = await c.post(url, headers=gh_headers(app_jwt))
        resp.raise_for_status()
        data = resp.json()

    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    return InstallationToken(token=data["token"], expires_at=expires_at)


async def resolve_installation_id(
    repo: str,
    *,
    app_id: str | None = None,
    private_key_pem: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Resolve the live installation id for an ``owner/repo`` via an App JWT.

    Installation ids rotate when the App is uninstalled/reinstalled, so a stored
    id eventually goes stale and 404s on token mint. GitHub's per-repo lookup
    (``GET /repos/{owner}/{repo}/installation``) always returns the current one,
    letting a reinstall self-heal instead of erroring every run.

    Args:
        repo: ``owner/repo`` slug.
        app_id: Override the configured App ID (used in tests).
        private_key_pem: Override the configured private key (used in tests).
        client: Injectable httpx client (used in tests).

    Returns:
        The current installation id for the repo.
    """
    resolved_app_id = app_id or github_app_id()
    resolved_key = private_key_pem or github_app_private_key()
    app_jwt = build_app_jwt(resolved_app_id, resolved_key)

    url = f"{github_api_url()}/repos/{repo}/installation"
    async with gh_client(client, timeout=15.0) as c:
        resp = await c.get(url, headers=gh_headers(app_jwt))
        resp.raise_for_status()
        return int(resp.json()["id"])


async def mint_token_for_repo(
    installation_id: int,
    repo: str,
    *,
    app_id: str | None = None,
    private_key_pem: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> InstallationToken:
    """Mint an installation token, self-healing a stale (reinstalled) id.

    Tries the stored ``installation_id`` first (keeping the happy path a single
    request); on a 404 — the App was uninstalled/reinstalled, rotating the id —
    it re-resolves the live id for the repo and retries once.

    Args:
        installation_id: The stored installation id to try first.
        repo: ``owner/repo`` slug, used to re-resolve a rotated id.
        app_id: Override the configured App ID (used in tests).
        private_key_pem: Override the configured private key (used in tests).
        client: Injectable httpx client (used in tests).
    """
    try:
        return await mint_installation_token(
            installation_id,
            app_id=app_id,
            private_key_pem=private_key_pem,
            client=client,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        live_id = await resolve_installation_id(
            repo, app_id=app_id, private_key_pem=private_key_pem, client=client
        )
        return await mint_installation_token(
            live_id,
            app_id=app_id,
            private_key_pem=private_key_pem,
            client=client,
        )
