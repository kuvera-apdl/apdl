"""Unit tests for GitHub App authentication.

Generates a throwaway RSA key per test — no real credentials, no network. The
installation-token exchange is exercised against an httpx MockTransport.
"""

from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.github.app_auth import (
    build_app_jwt,
    mint_installation_token,
    mint_token_for_repo,
    resolve_installation_id,
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
    # iat is backdated for clock skew; exp stays within GitHub's 10-minute ceiling.
    assert decoded["iat"] == int(now.timestamp()) - 60
    assert 0 < decoded["exp"] - int(now.timestamp()) <= 600


def test_build_app_jwt_requires_credentials():
    with pytest.raises(ValueError):
        build_app_jwt("", "")


@pytest.mark.asyncio
async def test_mint_installation_token_exchanges_jwt():
    private_pem, _ = _rsa_pem()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            201,
            json={"token": "ghs_faketoken", "expires_at": "2026-06-17T13:00:00Z"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await mint_installation_token(
            42, app_id="123456", private_key_pem=private_pem, client=client
        )

    assert result.token == "ghs_faketoken"
    assert result.expires_at == datetime(2026, 6, 17, 13, 0, tzinfo=timezone.utc)
    assert "/app/installations/42/access_tokens" in captured["url"]
    assert captured["auth"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_resolve_installation_id_looks_up_repo():
    private_pem, _ = _rsa_pem()
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"id": 99})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        live = await resolve_installation_id(
            "acme/widgets", app_id="123456", private_key_pem=private_pem, client=client
        )

    assert live == 99
    assert "/repos/acme/widgets/installation" in captured["url"]


@pytest.mark.asyncio
async def test_mint_token_for_repo_self_heals_on_404():
    private_pem, _ = _rsa_pem()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        # Stale stored id → 404; live-id lookup → 7; mint for the live id → token.
        if path == "/app/installations/1/access_tokens":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/repos/acme/widgets/installation":
            return httpx.Response(200, json={"id": 7})
        if path == "/app/installations/7/access_tokens":
            return httpx.Response(
                201, json={"token": "ghs_live", "expires_at": "2026-06-17T13:00:00Z"}
            )
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await mint_token_for_repo(
            1, "acme/widgets", app_id="123456", private_key_pem=private_pem, client=client
        )

    assert result.token == "ghs_live"
    assert seen == [
        "/app/installations/1/access_tokens",
        "/repos/acme/widgets/installation",
        "/app/installations/7/access_tokens",
    ]


@pytest.mark.asyncio
async def test_mint_token_for_repo_skips_lookup_on_success():
    private_pem, _ = _rsa_pem()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            201, json={"token": "ghs_stored", "expires_at": "2026-06-17T13:00:00Z"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await mint_token_for_repo(
            5, "acme/widgets", app_id="123456", private_key_pem=private_pem, client=client
        )

    # Happy path is a single request — no repo lookup.
    assert result.token == "ghs_stored"
    assert seen == ["/app/installations/5/access_tokens"]
