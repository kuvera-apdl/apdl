"""Unit tests for GitHub App authentication and scoped token issuance."""

import json
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.github.app_auth import (
    CODEGEN_PR_WRITE_PERMISSIONS,
    CODEGEN_READ_PERMISSIONS,
    CODEGEN_WRITE_PERMISSIONS,
    AuthorizedRepositoryTarget,
    _mint_token_for_repository,
    _revoke_installation_token,
    build_app_jwt,
    resolve_repository_target,
)


def _rsa_pem() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _token_response(
    repository_id: int,
    permissions: dict[str, str],
    *,
    token: str = "ghs_faketoken",
) -> dict:
    return {
        "token": token,
        "expires_at": "2026-06-17T13:00:00Z",
        "repositories": [{"id": repository_id, "full_name": "acme/widgets"}],
        "permissions": permissions,
    }


def test_build_app_jwt_has_expected_claims():
    private_pem, public_pem = _rsa_pem()
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    token = build_app_jwt("123456", private_pem, now=now)

    # Decode for claim inspection only: the token is signed with a fixed past
    # `now`, so skip wall-clock exp validation (we assert the window manually).
    decoded = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        options={"verify_aud": False, "verify_exp": False},
    )
    assert decoded["iss"] == "123456"
    assert decoded["iat"] == int(now.timestamp()) - 60
    assert 0 < decoded["exp"] - int(now.timestamp()) <= 600


def test_build_app_jwt_requires_credentials():
    with pytest.raises(ValueError):
        build_app_jwt("", "")


@pytest.mark.parametrize(
    ("installation_id", "repository_id"),
    [(0, 1), (-1, 1), (True, 1), (1, 0), (1, -1), (1, False)],
)
def test_authorized_repository_target_requires_strict_positive_ids(
    installation_id, repository_id
):
    with pytest.raises(ValueError):
        AuthorizedRepositoryTarget(
            installation_id=installation_id,
            repository_id=repository_id,
        )


@pytest.mark.parametrize(
    "permission_profile",
    [
        CODEGEN_READ_PERMISSIONS,
        CODEGEN_WRITE_PERMISSIONS,
        CODEGEN_PR_WRITE_PERMISSIONS,
    ],
    ids=["read", "contents-write", "pr-write"],
)
@pytest.mark.asyncio
async def test_mint_token_for_repository_requests_exact_numeric_scope(
    permission_profile,
):
    private_pem, _ = _rsa_pem()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            201,
            json=_token_response(987, dict(permission_profile)),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await _mint_token_for_repository(
            AuthorizedRepositoryTarget(installation_id=42, repository_id=987),
            permissions=permission_profile,
            app_id="123456",
            private_key_pem=private_pem,
            client=client,
        )

    assert result.token == "ghs_faketoken"
    assert captured["path"] == "/app/installations/42/access_tokens"
    assert captured["json"] == {
        "repository_ids": [987],
        "permissions": dict(permission_profile),
    }


@pytest.mark.asyncio
async def test_mint_token_for_repository_rejects_non_codegen_permission_profile():
    private_pem, _ = _rsa_pem()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: None)
    ) as client:
        with pytest.raises(ValueError, match="exact codegen permission profile"):
            await _mint_token_for_repository(
                AuthorizedRepositoryTarget(installation_id=42, repository_id=987),
                permissions={"contents": "write", "administration": "write"},
                app_id="123456",
                private_key_pem=private_pem,
                client=client,
            )


@pytest.mark.parametrize(
    "repositories",
    [
        [],
        [{"id": 111}],
        [{"id": 987}, {"id": 111}],
    ],
    ids=["missing", "wrong", "overbroad"],
)
@pytest.mark.asyncio
async def test_mint_token_for_repository_rejects_wrong_repository_scope(repositories):
    private_pem, _ = _rsa_pem()

    def handler(_: httpx.Request) -> httpx.Response:
        data = _token_response(987, dict(CODEGEN_WRITE_PERMISSIONS))
        data["repositories"] = repositories
        return httpx.Response(201, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="repository"):
            await _mint_token_for_repository(
                AuthorizedRepositoryTarget(installation_id=42, repository_id=987),
                permissions=CODEGEN_WRITE_PERMISSIONS,
                app_id="123456",
                private_key_pem=private_pem,
                client=client,
            )


@pytest.mark.asyncio
async def test_mint_token_for_repository_rejects_returned_permission_escalation():
    private_pem, _ = _rsa_pem()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "DELETE":
            assert request.headers["authorization"] == "Bearer ghs_faketoken"
            return httpx.Response(204)
        escalated = dict(CODEGEN_WRITE_PERMISSIONS)
        escalated["administration"] = "write"
        return httpx.Response(201, json=_token_response(987, escalated))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="unexpected permissions"):
            await _mint_token_for_repository(
                AuthorizedRepositoryTarget(installation_id=42, repository_id=987),
                permissions=CODEGEN_WRITE_PERMISSIONS,
                app_id="123456",
                private_key_pem=private_pem,
                client=client,
            )

    assert seen == ["POST", "DELETE"]


@pytest.mark.asyncio
async def test_mint_token_for_repository_fails_closed_on_stale_installation():
    private_pem, _ = _rsa_pem()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _mint_token_for_repository(
                AuthorizedRepositoryTarget(installation_id=1, repository_id=987),
                permissions=CODEGEN_WRITE_PERMISSIONS,
                app_id="123456",
                private_key_pem=private_pem,
                client=client,
            )

    assert seen == ["/app/installations/1/access_tokens"]


@pytest.mark.asyncio
async def test_revoke_installation_token_uses_credential_owned_endpoint():
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                request.headers["authorization"],
            )
        )
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await _revoke_installation_token("ghs_faketoken", client=client)

    assert seen == [
        ("DELETE", "/installation/token", "Bearer ghs_faketoken")
    ]


@pytest.mark.asyncio
async def test_resolve_repository_target_uses_name_scoped_read_only_token():
    private_pem, _ = _rsa_pem()
    seen: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.url.path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 42})
        if request.url.path == "/app/installations/42/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "ghs_discovery",
                    "expires_at": "2026-06-17T13:00:00Z",
                    "permissions": {"metadata": "read"},
                    "repositories": [{"id": 987, "full_name": "acme/widgets"}],
                },
            )
        if request.url.path == "/repos/acme/widgets":
            assert request.headers["authorization"] == "Bearer ghs_discovery"
            return httpx.Response(
                200,
                json={
                    "id": 987,
                    "full_name": "acme/widgets",
                    "default_branch": "trunk",
                },
            )
        if request.url.path == "/installation/token":
            assert request.headers["authorization"] == "Bearer ghs_discovery"
            return httpx.Response(204)
        raise AssertionError(f"unexpected path {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovered = await resolve_repository_target(
            "acme/widgets",
            app_id="123456",
            private_key_pem=private_pem,
            client=client,
        )

    assert discovered.installation_id == 42
    assert discovered.repository_id == 987
    assert discovered.repository_full_name == "acme/widgets"
    assert discovered.default_branch == "trunk"
    assert seen == [
        ("GET", "/repos/acme/widgets/installation", None),
        (
            "POST",
            "/app/installations/42/access_tokens",
            {
                "repositories": ["widgets"],
                "permissions": {"metadata": "read"},
            },
        ),
        ("GET", "/repos/acme/widgets", None),
        ("DELETE", "/installation/token", None),
    ]


@pytest.mark.asyncio
async def test_resolve_repository_target_rejects_identity_change():
    private_pem, _ = _rsa_pem()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 42})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "ghs_discovery",
                    "expires_at": "2026-06-17T13:00:00Z",
                    "permissions": {"metadata": "read"},
                    "repositories": [{"id": 987, "full_name": "acme/widgets"}],
                },
            )
        if request.url.path == "/installation/token":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "id": 654,
                "full_name": "acme/widgets",
                "default_branch": "main",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="identity changed"):
            await resolve_repository_target(
                "acme/widgets",
                app_id="123456",
                private_key_pem=private_pem,
                client=client,
            )


@pytest.mark.parametrize(
    "repo", ["widgets", "acme/widgets/extra", " acme/widgets", "a/b?x"]
)
@pytest.mark.asyncio
async def test_resolve_repository_target_rejects_invalid_slug(repo):
    private_pem, _ = _rsa_pem()
    with pytest.raises(ValueError, match="owner/name"):
        await resolve_repository_target(
            repo,
            app_id="123456",
            private_key_pem=private_pem,
        )
