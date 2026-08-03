"""Tests for least-privilege GitHub user repository discovery."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import httpx
import pytest

from app.github.app_auth import (
    CODEGEN_METADATA_PERMISSIONS,
    AuthorizedRepositoryTarget,
    InstallationToken,
)
from app.github.user_authorization import (
    REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS,
    GitHubAuthorizationSettings,
    discover_user_repositories,
    verify_repository_candidate,
)
from app.models.repository_authorization import DiscoveredRepository

_CALLBACK_URL = "https://admin.example.test/api/github/codegen/callback"


def _settings() -> GitHubAuthorizationSettings:
    return GitHubAuthorizationSettings(
        app_id=123,
        app_slug="apdl-test",
        client_id="Iv1.client",
        client_secret="client-secret",
        callback_url=_CALLBACK_URL,
    )


def _configure_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_SLUG", "apdl-test")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GITHUB_APP_CALLBACK_URL", _CALLBACK_URL)
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY_BASE64",
        base64.b64encode(b"test-private-key").decode("ascii"),
    )


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("GITHUB_APP_ID", " 123"),
        ("GITHUB_APP_ID", "123 "),
        ("GITHUB_APP_ID", "00123"),
        ("GITHUB_APP_SLUG", " apdl-test"),
        ("GITHUB_APP_SLUG", "apdl-test "),
        ("GITHUB_APP_CLIENT_ID", " Iv1.client"),
        ("GITHUB_APP_CLIENT_ID", "Iv1.client "),
        ("GITHUB_APP_CLIENT_ID", "Iv1.client\n"),
        ("GITHUB_APP_CLIENT_ID", "Iv1.client\x7f"),
        ("GITHUB_APP_CALLBACK_URL", f" {_CALLBACK_URL}"),
        ("GITHUB_APP_CALLBACK_URL", f"{_CALLBACK_URL} "),
    ],
)
def test_authorization_settings_reject_noncanonical_whitespace_and_ids(
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str,
) -> None:
    _configure_settings(monkeypatch)
    monkeypatch.setenv(setting, value)

    with pytest.raises(ValueError, match=setting):
        GitHubAuthorizationSettings.from_environment()


@pytest.mark.parametrize(
    "secret",
    ["", "client secret", "client-secret\n", "client-secret\x7f"],
    ids=["missing", "space", "newline", "control"],
)
def test_authorization_settings_reject_invalid_client_secret(
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
) -> None:
    _configure_settings(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", secret)

    with pytest.raises(ValueError, match="GITHUB_APP_CLIENT_SECRET"):
        GitHubAuthorizationSettings.from_environment()


def test_authorization_settings_accept_maximum_github_app_id(monkeypatch) -> None:
    _configure_settings(monkeypatch)
    maximum = 9_223_372_036_854_775_807
    monkeypatch.setenv("GITHUB_APP_ID", str(maximum))

    assert GitHubAuthorizationSettings.from_environment().app_id == maximum


def test_authorization_settings_reject_out_of_range_github_app_id(monkeypatch) -> None:
    _configure_settings(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_ID", "9223372036854775808")

    with pytest.raises(ValueError, match="GITHUB_APP_ID"):
        GitHubAuthorizationSettings.from_environment()


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://admin.example.test/api/github/codegen/callback/",
        "https://admin.example.test/api/github/callback",
        "https://admin.example.test/api/github/codegen/callback?next=/",
        "https://admin.example.test/api/github/codegen/callback#fragment",
        "http://admin.example.test/api/github/codegen/callback",
        "https://" + "a" * 2_100 + "/api/github/codegen/callback",
    ],
    ids=[
        "trailing-slash",
        "wrong-path",
        "query",
        "fragment",
        "remote-http",
        "too-long",
    ],
)
def test_authorization_settings_require_exact_callback_contract(
    monkeypatch: pytest.MonkeyPatch,
    callback_url: str,
) -> None:
    _configure_settings(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_CALLBACK_URL", callback_url)

    with pytest.raises(ValueError, match="GITHUB_APP_CALLBACK_URL"):
        GitHubAuthorizationSettings.from_environment()


def test_authorization_settings_allow_http_loopback_callback(monkeypatch) -> None:
    _configure_settings(monkeypatch)
    callback_url = "http://localhost:5173/api/github/codegen/callback"
    monkeypatch.setenv("GITHUB_APP_CALLBACK_URL", callback_url)

    assert GitHubAuthorizationSettings.from_environment().callback_url == callback_url


@pytest.mark.parametrize("private_key", [None, "not-base64"])
def test_authorization_settings_require_decodable_private_key(
    monkeypatch: pytest.MonkeyPatch,
    private_key: str | None,
) -> None:
    _configure_settings(monkeypatch)
    if private_key is None:
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_BASE64")
    else:
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", private_key)

    with pytest.raises(ValueError, match="GITHUB_APP_PRIVATE_KEY_BASE64"):
        GitHubAuthorizationSettings.from_environment()


@pytest.mark.asyncio
async def test_discovery_keeps_only_writable_admin_repositories_and_revokes_token(
    monkeypatch,
):
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 77, "login": "octocat"})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 42,
                            "app_id": 123,
                            "permissions": dict(
                                REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS
                            ),
                            "suspended_at": None,
                        },
                        {
                            "id": 43,
                            "app_id": 123,
                            "permissions": dict(
                                REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS
                            ),
                            "suspended_at": "2026-08-03T12:00:00Z",
                        },
                        {
                            "id": 44,
                            "app_id": 123,
                            "permissions": {"metadata": "read"},
                            "suspended_at": None,
                        },
                    ]
                },
            )
        if request.url.path == "/user/installations/42/repositories":
            base = {
                "default_branch": "main",
                "private": True,
                "archived": False,
                "disabled": False,
                "permissions": {"admin": True},
            }
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {**base, "id": 987, "full_name": "acme/widgets"},
                        {
                            **base,
                            "id": 988,
                            "full_name": "acme/read-only",
                            "permissions": {"admin": False},
                        },
                        {
                            **base,
                            "id": 989,
                            "full_name": "acme/archived",
                            "archived": True,
                        },
                        {
                            **base,
                            "id": 990,
                            "full_name": "acme/disabled",
                            "disabled": True,
                        },
                    ]
                },
            )
        if request.url.path == "/applications/Iv1.client/token":
            assert request.method == "DELETE"
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(204)
        raise AssertionError(f"Unexpected GitHub request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        user, repositories = await discover_user_repositories(
            "ghu_ephemeral",
            _settings(),
            client=client,
        )

    assert (user.id, user.login) == (77, "octocat")
    assert [item.repository_full_name for item in repositories] == [
        "acme/widgets"
    ]
    assert repositories[0].installation_id == 42
    assert "/user/installations/43/repositories" not in requested_paths
    assert "/user/installations/44/repositories" not in requested_paths
    assert requested_paths[-1] == "/applications/Iv1.client/token"


@pytest.mark.asyncio
async def test_discovery_rejects_incomplete_bounded_pagination_and_revokes_token(
    monkeypatch,
):
    revocations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal revocations
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 77, "login": "octocat"})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                headers={
                    "Link": (
                        '<https://api.github.com/user/installations?page=2>; '
                        'rel="next"'
                    )
                },
                json={"installations": []},
            )
        if request.url.path == "/applications/Iv1.client/token":
            revocations += 1
            return httpx.Response(204)
        raise AssertionError(f"Unexpected GitHub request: {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="pagination exceeded"):
            await discover_user_repositories(
                "ghu_ephemeral",
                _settings(),
                client=client,
            )

    assert revocations == 1


@pytest.mark.asyncio
async def test_completion_verifier_checks_live_installation_and_exact_repository(
    monkeypatch,
):
    import app.github.user_authorization as user_authorization

    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY_BASE64",
        base64.b64encode(b"test-private-key").decode(),
    )
    monkeypatch.setattr(
        user_authorization,
        "build_app_jwt",
        lambda app_id, private_key: "app-jwt",
    )
    revoked: list[str] = []

    async def mint(target, *, permissions, app_id, private_key_pem, client):
        assert target == AuthorizedRepositoryTarget(
            installation_id=42,
            repository_id=987,
        )
        assert permissions == CODEGEN_METADATA_PERMISSIONS
        assert app_id == "123"
        assert private_key_pem == "test-private-key"
        assert client is not None
        return InstallationToken(
            token="ghs_verification",
            expires_at=datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc),
        )

    async def revoke(token, client, *, context):
        assert client is not None
        assert context == "live repository authorization verification"
        revoked.append(token)

    monkeypatch.setattr(user_authorization, "_mint_token_for_repository", mint)
    monkeypatch.setattr(user_authorization, "_best_effort_revoke_token", revoke)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/42":
            assert request.headers["authorization"] == "Bearer app-jwt"
            return httpx.Response(
                200,
                json={
                    "id": 42,
                    "app_id": 123,
                    "suspended_at": None,
                    "permissions": dict(
                        REQUIRED_CODEGEN_INSTALLATION_PERMISSIONS
                    ),
                },
            )
        if request.url.path == "/repositories/987":
            assert request.headers["authorization"] == "Bearer ghs_verification"
            return httpx.Response(
                200,
                json={
                    "id": 987,
                    "full_name": "acme/widgets",
                    "default_branch": "main",
                    "private": True,
                    "archived": False,
                    "disabled": False,
                },
            )
        raise AssertionError(f"Unexpected GitHub request: {request.url}")

    candidate = DiscoveredRepository(
        installation_id=42,
        repository_id=987,
        repository_full_name="acme/widgets",
        default_base_branch="main",
        private=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        await verify_repository_candidate(
            candidate,
            _settings(),
            client=client,
        )

    assert revoked == ["ghs_verification"]
