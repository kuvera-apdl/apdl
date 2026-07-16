"""GitHub App authentication and repository-scoped token issuance.

Write-capable installation tokens are minted only for an already-authorized
numeric repository id.  Repository slugs are deliberately absent from that
path: a slug lookup proves only that the shared GitHub App can see a repository,
not that an APDL project is authorized to mutate it.

The slug-based helper in this module is for trusted operator discovery only. It
mints a repository-scoped metadata token and must not be exposed through tenant
routes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from urllib.parse import quote

import httpx
import jwt

from app.config import github_api_url, github_app_id, github_app_private_key
from app.github.client import gh_client, gh_headers

logger = logging.getLogger(__name__)

#: GitHub rejects App JWTs whose ``exp`` is more than 10 minutes out. Use a
#: conservative 9-minute window and backdate ``iat`` by 60s for clock skew.
_JWT_TTL = timedelta(minutes=9)
_CLOCK_SKEW = timedelta(seconds=60)

#: Exact least-privilege profiles used by codegen.  Callers choose one of these
#: profiles; arbitrary permission mappings are rejected by the token broker.
CODEGEN_READ_PERMISSIONS: Mapping[str, str] = MappingProxyType(
    {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "metadata": "read",
        "pull_requests": "read",
        "statuses": "read",
    }
)
CODEGEN_WRITE_PERMISSIONS: Mapping[str, str] = MappingProxyType(
    {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
)
CODEGEN_PR_WRITE_PERMISSIONS: Mapping[str, str] = MappingProxyType(
    {
        "metadata": "read",
        "pull_requests": "write",
    }
)
_DISCOVERY_PERMISSIONS: Mapping[str, str] = MappingProxyType({"metadata": "read"})


def _positive_github_id(value: object, *, field: str) -> int:
    """Return a strict positive GitHub id (booleans are not integer ids)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class AuthorizedRepositoryTarget:
    """Immutable GitHub authority already granted to an APDL project.

    Constructing this value does not itself prove project authorization.  The
    caller must load it from the project's active repository grant rather than
    from a request payload.
    """

    installation_id: int
    repository_id: int

    def __post_init__(self) -> None:
        _positive_github_id(self.installation_id, field="installation_id")
        _positive_github_id(self.repository_id, field="repository_id")


@dataclass(frozen=True)
class DiscoveredRepositoryTarget:
    """Repository identity resolved during a trusted operator grant flow."""

    installation_id: int
    repository_id: int
    repository_full_name: str
    default_branch: str

@dataclass(frozen=True)
class InstallationToken:
    """A minted installation access token and its expiry (UTC)."""

    token: str
    expires_at: datetime


def build_app_jwt(
    app_id: str, private_key_pem: str, *, now: datetime | None = None
) -> str:
    """Build a signed App JWT (RS256) for authenticating as the GitHub App."""
    if not app_id or not private_key_pem:
        raise ValueError("GitHub App ID and private key are required to mint a JWT.")
    moment = now or datetime.now(timezone.utc)
    payload = {
        "iat": int((moment - _CLOCK_SKEW).timestamp()),
        "exp": int((moment + _JWT_TTL).timestamp()),
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def _parse_token(data: object) -> InstallationToken:
    if not isinstance(data, dict):
        raise ValueError("GitHub installation-token response must be an object")
    token = data.get("token")
    raw_expiry = data.get("expires_at")
    if not isinstance(token, str) or not token:
        raise ValueError("GitHub installation-token response is missing token")
    if not isinstance(raw_expiry, str) or not raw_expiry:
        raise ValueError("GitHub installation-token response is missing expires_at")
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "GitHub installation-token response has invalid expires_at"
        ) from exc
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("GitHub installation-token expires_at must include a timezone")
    return InstallationToken(token=token, expires_at=expires_at)


def _raw_issued_token(data: object) -> str | None:
    """Extract a credential only so a rejected GitHub response can be revoked."""
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    return token if isinstance(token, str) and token else None


async def _revoke_token_with_client(token: str, client: httpx.AsyncClient) -> None:
    response = await client.delete(
        f"{github_api_url()}/installation/token",
        headers=gh_headers(token),
    )
    response.raise_for_status()


async def _best_effort_revoke_token(
    token: str,
    client: httpx.AsyncClient,
    *,
    context: str,
) -> None:
    """Attempt cleanup without replacing the operation's authoritative result."""
    try:
        await _revoke_token_with_client(token, client)
    except Exception:
        logger.exception("Could not revoke GitHub token after %s", context)


def _permission_payload(permissions: Mapping[str, str]) -> dict[str, str]:
    try:
        requested = dict(permissions)
    except (TypeError, ValueError) as exc:
        raise ValueError("permissions must be a codegen permission profile") from exc
    if requested not in (
        dict(CODEGEN_READ_PERMISSIONS),
        dict(CODEGEN_WRITE_PERMISSIONS),
        dict(CODEGEN_PR_WRITE_PERMISSIONS),
    ):
        raise ValueError("permissions must be an exact codegen permission profile")
    return requested


def _returned_permissions(data: Mapping[object, object]) -> dict[str, str]:
    raw = data.get("permissions")
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise ValueError("GitHub installation-token response has invalid permissions")
    return dict(raw)


def _validate_repository_scope(
    data: object,
    *,
    repository_id: int,
    permissions: Mapping[str, str],
) -> InstallationToken:
    """Fail closed unless GitHub returned exactly the requested authority."""
    if not isinstance(data, dict):
        raise ValueError("GitHub installation-token response must be an object")
    repositories = data.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise ValueError(
            "GitHub installation token must be scoped to exactly one repository"
        )
    repository = repositories[0]
    if not isinstance(repository, dict):
        raise ValueError("GitHub installation-token repository must be an object")
    returned_id = _positive_github_id(
        repository.get("id"), field="returned repository id"
    )
    if returned_id != repository_id:
        raise ValueError("GitHub installation token returned an unexpected repository")
    if _returned_permissions(data) != dict(permissions):
        raise ValueError("GitHub installation token returned unexpected permissions")
    return _parse_token(data)


async def _mint_token_for_repository(
    target: AuthorizedRepositoryTarget,
    *,
    permissions: Mapping[str, str],
    app_id: str | None = None,
    private_key_pem: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> InstallationToken:
    """Mint a token for one pre-authorized numeric repository id.

    ``target`` must come from an active project repository grant.  GitHub's
    response is checked before the credential is returned, so an over-broad or
    mismatched token never reaches a clone, push, PR, poll, or repair operation.
    Installation rotation fails closed and requires explicit reauthorization;
    this function never re-resolves an installation from a slug.
    """
    if not isinstance(target, AuthorizedRepositoryTarget):
        raise TypeError("target must be an AuthorizedRepositoryTarget")
    requested_permissions = _permission_payload(permissions)
    resolved_app_id = app_id or github_app_id()
    resolved_key = private_key_pem or github_app_private_key()
    app_jwt = build_app_jwt(resolved_app_id, resolved_key)

    url = f"{github_api_url()}/app/installations/{target.installation_id}/access_tokens"
    async with gh_client(client, timeout=15.0) as c:
        resp = await c.post(
            url,
            headers=gh_headers(app_jwt),
            json={
                "repository_ids": [target.repository_id],
                "permissions": requested_permissions,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            return _validate_repository_scope(
                data,
                repository_id=target.repository_id,
                permissions=requested_permissions,
            )
        except Exception:
            rejected_token = _raw_issued_token(data)
            if rejected_token is not None:
                await _best_effort_revoke_token(
                    rejected_token,
                    c,
                    context="rejecting an invalid issuance response",
                )
            raise


async def _revoke_installation_token(
    token: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Immediately revoke one installation access token.

    This primitive is private for the same reason as token minting: callers
    must receive credentials only through the DB-backed broker, which owns the
    token lifetime and invokes this endpoint when a lease exits.
    """
    if not isinstance(token, str) or not token:
        raise ValueError("GitHub installation token is required for revocation")
    async with gh_client(client, timeout=15.0) as c:
        await _revoke_token_with_client(token, c)


def _parse_repo_slug(repo: str) -> tuple[str, str]:
    if not isinstance(repo, str):
        raise ValueError("repository must be an owner/name slug")
    parts = repo.split("/")
    if (
        len(parts) != 2
        or any(not part or part.strip() != part for part in parts)
        or any(char in repo for char in "?#\\")
    ):
        raise ValueError("repository must be an owner/name slug")
    return parts[0], parts[1]


async def resolve_repository_target(
    repo: str,
    *,
    app_id: str | None = None,
    private_key_pem: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> DiscoveredRepositoryTarget:
    """Resolve immutable repository identity for a trusted operator grant flow.

    The name-scoped token is read-only and used once to fetch canonical metadata.
    Returning this discovery result does not create or activate a project grant.
    """
    owner, name = _parse_repo_slug(repo)
    resolved_app_id = app_id or github_app_id()
    resolved_key = private_key_pem or github_app_private_key()
    app_jwt = build_app_jwt(resolved_app_id, resolved_key)
    encoded_repo = f"{quote(owner, safe='')}/{quote(name, safe='')}"

    async with gh_client(client, timeout=15.0) as c:
        installation_resp = await c.get(
            f"{github_api_url()}/repos/{encoded_repo}/installation",
            headers=gh_headers(app_jwt),
        )
        installation_resp.raise_for_status()
        installation_data = installation_resp.json()
        if not isinstance(installation_data, dict):
            raise ValueError("GitHub installation response must be an object")
        installation_id = _positive_github_id(
            installation_data.get("id"), field="installation_id"
        )

        token_resp = await c.post(
            f"{github_api_url()}/app/installations/{installation_id}/access_tokens",
            headers=gh_headers(app_jwt),
            json={
                "repositories": [name],
                "permissions": dict(_DISCOVERY_PERMISSIONS),
            },
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        try:
            if not isinstance(token_data, dict):
                raise ValueError("GitHub installation-token response must be an object")
            if _returned_permissions(token_data) != dict(_DISCOVERY_PERMISSIONS):
                raise ValueError("GitHub discovery token returned unexpected permissions")
            repositories = token_data.get("repositories")
            if not isinstance(repositories, list) or len(repositories) != 1:
                raise ValueError(
                    "GitHub discovery token must select exactly one repository"
                )
            scoped_repo = repositories[0]
            if not isinstance(scoped_repo, dict):
                raise ValueError("GitHub discovery repository must be an object")
            scoped_id = _positive_github_id(
                scoped_repo.get("id"), field="returned repository id"
            )
            scoped_full_name = scoped_repo.get("full_name")
            if (
                not isinstance(scoped_full_name, str)
                or scoped_full_name.casefold() != repo.casefold()
            ):
                raise ValueError(
                    "GitHub discovery token returned an unexpected repository"
                )
            discovery_token = _parse_token(token_data)
            metadata_resp = await c.get(
                f"{github_api_url()}/repos/{encoded_repo}",
                headers=gh_headers(discovery_token.token),
            )
            metadata_resp.raise_for_status()
            metadata = metadata_resp.json()
        finally:
            issued_token = _raw_issued_token(token_data)
            if issued_token is not None:
                await _best_effort_revoke_token(
                    issued_token,
                    c,
                    context="repository discovery",
                )

    if not isinstance(metadata, dict):
        raise ValueError("GitHub repository response must be an object")
    repository_id = _positive_github_id(metadata.get("id"), field="repository_id")
    repository_full_name = metadata.get("full_name")
    default_branch = metadata.get("default_branch")
    if repository_id != scoped_id:
        raise ValueError("GitHub repository identity changed during discovery")
    if (
        not isinstance(repository_full_name, str)
        or repository_full_name != scoped_full_name
    ):
        raise ValueError("GitHub repository name changed during discovery")
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError("GitHub repository response is missing default_branch")
    return DiscoveredRepositoryTarget(
        installation_id=installation_id,
        repository_id=repository_id,
        repository_full_name=repository_full_name,
        default_branch=default_branch,
    )
