"""Short-lived GitHub App user OAuth discovery for repository onboarding.

This flow deliberately uses a GitHub *user* access token to enumerate only the
intersection of repositories visible to the App and administered by that user.
It never performs the App-wide installation enumeration that made the original
operator-only stopgap necessary.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import quote, urlencode

import httpx

from app.config import (
    GITHUB_API_URL,
    GITHUB_WEB_URL,
    github_app_callback_url,
    github_app_client_id,
    github_app_client_secret,
    github_app_id,
    github_app_private_key,
    github_app_slug,
)
from app.github.client import gh_client, gh_headers, github_paginated_items
from app.github.app_auth import (
    CODEGEN_METADATA_PERMISSIONS,
    AuthorizedRepositoryTarget,
    _best_effort_revoke_token,
    _mint_token_for_repository,
    build_app_jwt,
)
from app.models.repository_authorization import DiscoveredRepository

logger = logging.getLogger(__name__)

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,98}[A-Za-z0-9])?$")
_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_PKCE_VERIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_PKCE_CHALLENGE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_APP_ID_PATTERN = re.compile(r"^[1-9][0-9]*$")
_CALLBACK_PATH = "/api/github/codegen/callback"
_MAX_CALLBACK_URL_LENGTH = 2_048
_MAX_GITHUB_ID = 9_223_372_036_854_775_807
_MAX_INSTALLATION_PAGES = 10
_MAX_REPOSITORY_PAGES = 10
_MAX_INSTALLATIONS = 100
_MAX_REPOSITORIES = 1_000

# The union of every permission profile Codegen may request after connection.
# An installation lacking any one of these must not be offered as usable.
REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS = MappingProxyType(
    {
        "actions": "read",
        "checks": "read",
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
        "statuses": "read",
    }
)
_PERMISSION_RANK = {"read": 1, "write": 2, "admin": 3}


def _validated_callback_url(raw: str) -> str:
    if not raw or len(raw) > _MAX_CALLBACK_URL_LENGTH or raw.strip() != raw or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in raw
    ):
        raise ValueError(
            "GITHUB_APP_CALLBACK_URL must be a canonical absolute callback URL"
        )
    try:
        url = httpx.URL(raw)
    except Exception as exc:
        raise ValueError(
            "GITHUB_APP_CALLBACK_URL must be an absolute HTTP(S) URL"
        ) from exc
    loopback = url.host in {"localhost", "127.0.0.1", "::1"}
    if (
        url.scheme not in {"http", "https"}
        or (url.scheme != "https" and not loopback)
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path != _CALLBACK_PATH
    ):
        raise ValueError(
            "GITHUB_APP_CALLBACK_URL must use the exact path "
            f"{_CALLBACK_PATH!r} on an HTTPS origin or HTTP loopback origin"
        )
    return str(url)


def _is_visible_ascii(value: str, *, max_length: int) -> bool:
    return (
        bool(value)
        and len(value) <= max_length
        and value.isascii()
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def _positive_id(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"GitHub {field} must be a positive integer")
    return value


@dataclass(frozen=True)
class GitHubAuthorizationSettings:
    """Validated GitHub App OAuth configuration."""

    app_id: int
    app_slug: str
    client_id: str
    client_secret: str
    callback_url: str

    @classmethod
    def from_environment(cls) -> GitHubAuthorizationSettings:
        raw_app_id = github_app_id()
        if _APP_ID_PATTERN.fullmatch(raw_app_id) is None:
            raise ValueError("GITHUB_APP_ID must be a canonical positive integer")
        app_id = int(raw_app_id)
        if app_id > _MAX_GITHUB_ID:
            raise ValueError("GITHUB_APP_ID exceeds the supported GitHub ID range")

        slug = github_app_slug()
        if _SLUG_PATTERN.fullmatch(slug) is None:
            raise ValueError("GITHUB_APP_SLUG is missing or invalid")
        client_id = github_app_client_id()
        client_secret = github_app_client_secret()
        if not _is_visible_ascii(client_id, max_length=255):
            raise ValueError("GITHUB_APP_CLIENT_ID is missing or invalid")
        if not _is_visible_ascii(client_secret, max_length=1024):
            raise ValueError("GITHUB_APP_CLIENT_SECRET is missing or invalid")
        if not github_app_private_key():
            raise ValueError(
                "GITHUB_APP_PRIVATE_KEY_BASE64 is missing or invalid"
            )
        return cls(
            app_id=app_id,
            app_slug=slug,
            client_id=client_id,
            client_secret=client_secret,
            callback_url=_validated_callback_url(github_app_callback_url()),
        )

    def installation_url(self, state: str) -> str:
        query = urlencode({"state": state})
        slug = quote(self.app_slug, safe="")
        return f"{GITHUB_WEB_URL}/apps/{slug}/installations/new?{query}"

    def oauth_url(self, state: str, code_challenge: str) -> str:
        if _PKCE_CHALLENGE_PATTERN.fullmatch(code_challenge) is None:
            raise ValueError("PKCE code challenge is invalid")
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.callback_url,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{GITHUB_WEB_URL}/login/oauth/authorize?{query}"


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str


def derive_pkce_verifier(oauth_state: str, client_secret: str) -> str:
    """Derive a replica-safe RFC 7636 verifier from state and server secret."""
    if re.fullmatch(r"[A-Za-z0-9_-]{32,512}", oauth_state) is None:
        raise ValueError("OAuth state is invalid for PKCE derivation")
    if not client_secret:
        raise ValueError("GitHub App client secret is required for PKCE")
    digest = hmac.new(
        client_secret.encode("utf-8"),
        b"apdl-github-repository-authorization-pkce@1\x00"
        + oauth_state.encode("ascii"),
        hashlib.sha256,
    ).digest()
    verifier = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    if _PKCE_VERIFIER_PATTERN.fullmatch(verifier) is None:  # pragma: no cover
        raise RuntimeError("Derived PKCE verifier was not canonical")
    return verifier


def pkce_code_challenge(verifier: str) -> str:
    """Return the unpadded base64url SHA-256 challenge for one verifier."""
    if _PKCE_VERIFIER_PATTERN.fullmatch(verifier) is None:
        raise ValueError("PKCE code verifier is invalid")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _installation_has_required_permissions(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for name, required in REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS.items():
        actual = value.get(name)
        if not isinstance(actual, str):
            return False
        if _PERMISSION_RANK.get(actual, 0) < _PERMISSION_RANK[required]:
            return False
    return True


def _parse_user(payload: object) -> GitHubUser:
    if not isinstance(payload, dict):
        raise ValueError("GitHub user response must be an object")
    github_user_id = _positive_id(payload.get("id"), field="user id")
    login = payload.get("login")
    if not isinstance(login, str) or _LOGIN_PATTERN.fullmatch(login) is None:
        raise ValueError("GitHub user response has an invalid login")
    return GitHubUser(id=github_user_id, login=login)


def _parse_repository(
    payload: object,
    *,
    installation_id: int,
) -> DiscoveredRepository | None:
    if not isinstance(payload, dict):
        raise ValueError("GitHub repository response must be an object")
    permissions = payload.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("admin") is not True:
        return None
    if payload.get("archived") is True or payload.get("disabled") is True:
        return None
    private = payload.get("private")
    if not isinstance(private, bool):
        raise ValueError("GitHub repository response has invalid visibility")
    return DiscoveredRepository(
        installation_id=installation_id,
        repository_id=_positive_id(payload.get("id"), field="repository id"),
        repository_full_name=payload.get("full_name"),
        default_base_branch=payload.get("default_branch"),
        private=private,
    )


async def exchange_oauth_code(
    code: str,
    code_verifier: str,
    settings: GitHubAuthorizationSettings,
    *,
    client: httpx.AsyncClient,
) -> str:
    """Exchange one one-time OAuth code without logging or persisting its token."""
    if _PKCE_VERIFIER_PATTERN.fullmatch(code_verifier) is None:
        raise ValueError("PKCE code verifier is invalid")
    response = await client.post(
        f"{GITHUB_WEB_URL}/login/oauth/access_token",
        headers={"Accept": "application/json"},
        data={
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": settings.callback_url,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("GitHub OAuth response must be an object")
    token = payload.get("access_token")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 1024
        or any(character.isspace() for character in token)
    ):
        error = payload.get("error")
        if isinstance(error, str) and error:
            raise ValueError("GitHub rejected the OAuth code")
        raise ValueError("GitHub OAuth response is missing an access token")
    return token


async def _best_effort_revoke_user_token(
    token: str,
    settings: GitHubAuthorizationSettings,
    *,
    client: httpx.AsyncClient,
) -> None:
    try:
        response = await client.request(
            "DELETE",
            f"{GITHUB_API_URL}/applications/{quote(settings.client_id, safe='')}/token",
            auth=httpx.BasicAuth(settings.client_id, settings.client_secret),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"access_token": token},
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Could not revoke GitHub user token after repository discovery")


async def discover_user_repositories(
    token: str,
    settings: GitHubAuthorizationSettings,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[GitHubUser, list[DiscoveredRepository]]:
    """Discover only admin repositories exposed to this App/user intersection."""
    repositories: list[DiscoveredRepository] = []
    seen_repository_ids: set[int] = set()
    async with gh_client(client, timeout=20.0) as github:
        try:
            user_response = await github.get(
                f"{GITHUB_API_URL}/user",
                headers=gh_headers(token),
            )
            user_response.raise_for_status()
            user = _parse_user(user_response.json())

            installations = await github_paginated_items(
                github,
                f"{GITHUB_API_URL}/user/installations?per_page=100",
                token,
                "installations",
                max_pages=_MAX_INSTALLATION_PAGES,
            )
            if len(installations) > _MAX_INSTALLATIONS:
                raise ValueError("GitHub user has too many App installations")

            for installation in installations:
                if not isinstance(installation, dict):
                    raise ValueError("GitHub installation response must be an object")
                if installation.get("suspended_at") is not None:
                    continue
                if _positive_id(installation.get("app_id"), field="App id") != (
                    settings.app_id
                ):
                    raise ValueError("GitHub returned an installation for another App")
                if not _installation_has_required_permissions(
                    installation.get("permissions")
                ):
                    continue
                installation_id = _positive_id(
                    installation.get("id"), field="installation id"
                )
                items = await github_paginated_items(
                    github,
                    (
                        f"{GITHUB_API_URL}/user/installations/"
                        f"{installation_id}/repositories?per_page=100"
                    ),
                    token,
                    "repositories",
                    max_pages=_MAX_REPOSITORY_PAGES,
                )
                if len(items) > _MAX_REPOSITORIES:
                    raise ValueError("GitHub installation has too many repositories")
                for item in items:
                    repository = _parse_repository(
                        item,
                        installation_id=installation_id,
                    )
                    if repository is None:
                        continue
                    if repository.repository_id in seen_repository_ids:
                        raise ValueError("GitHub returned a repository more than once")
                    seen_repository_ids.add(repository.repository_id)
                    repositories.append(repository)
                    if len(repositories) > _MAX_REPOSITORIES:
                        raise ValueError("GitHub user has too many repositories")
        finally:
            await _best_effort_revoke_user_token(
                token,
                settings,
                client=github,
            )

    repositories.sort(key=lambda item: item.repository_full_name.casefold())
    return user, repositories


async def verify_repository_candidate(
    candidate: DiscoveredRepository,
    settings: GitHubAuthorizationSettings,
    *,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Revalidate one stored candidate against the live GitHub App authority."""
    private_key = github_app_private_key()
    app_jwt = build_app_jwt(str(settings.app_id), private_key)
    target = AuthorizedRepositoryTarget(
        installation_id=candidate.installation_id,
        repository_id=candidate.repository_id,
    )
    async with gh_client(client, timeout=20.0) as github:
        installation_response = await github.get(
            f"{GITHUB_API_URL}/app/installations/{candidate.installation_id}",
            headers=gh_headers(app_jwt),
        )
        installation_response.raise_for_status()
        installation = installation_response.json()
        if not isinstance(installation, dict):
            raise ValueError("GitHub installation response must be an object")
        if _positive_id(installation.get("id"), field="installation id") != (
            candidate.installation_id
        ):
            raise ValueError("GitHub installation identity changed")
        if _positive_id(installation.get("app_id"), field="App id") != settings.app_id:
            raise ValueError("GitHub installation belongs to another App")
        if installation.get("suspended_at") is not None:
            raise ValueError("GitHub installation is suspended")
        if not _installation_has_required_permissions(
            installation.get("permissions")
        ):
            raise ValueError("GitHub installation lacks Codegen permissions")

        token = await _mint_token_for_repository(
            target,
            permissions=CODEGEN_METADATA_PERMISSIONS,
            app_id=str(settings.app_id),
            private_key_pem=private_key,
            client=github,
        )
        try:
            repository_response = await github.get(
                f"{GITHUB_API_URL}/repositories/{candidate.repository_id}",
                headers=gh_headers(token.token),
            )
            repository_response.raise_for_status()
            repository = repository_response.json()
            if not isinstance(repository, dict):
                raise ValueError("GitHub repository response must be an object")
            if _positive_id(repository.get("id"), field="repository id") != (
                candidate.repository_id
            ):
                raise ValueError("GitHub repository identity changed")
            if repository.get("full_name") != candidate.repository_full_name:
                raise ValueError("GitHub repository name changed")
            if repository.get("default_branch") != candidate.default_base_branch:
                raise ValueError("GitHub repository default branch changed")
            if repository.get("private") is not candidate.private:
                raise ValueError("GitHub repository visibility changed")
            if repository.get("archived") is not False:
                raise ValueError("GitHub repository is archived")
            if repository.get("disabled") is not False:
                raise ValueError("GitHub repository is disabled")
        finally:
            await _best_effort_revoke_token(
                token.token,
                github,
                context="live repository authorization verification",
            )
