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
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    return InstallationToken(token=data["token"], expires_at=expires_at)
