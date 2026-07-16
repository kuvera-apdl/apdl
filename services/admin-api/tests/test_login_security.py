from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import Request, Response

from app.login_security import (
    DEVICE_COOKIE,
    build_login_source,
    preflight_login,
    progressive_delay_seconds,
    resolve_client_ip,
    set_device_cookie,
)


def _settings(**overrides):
    values = {
        "trusted_proxy_cidrs": (
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
        ),
        "login_risk_hmac_key": "test-login-risk-key-with-at-least-32-bytes",
        "cookie_secure": True,
        "login_device_ttl_seconds": 31_536_000,
        "login_progressive_failure_threshold": 3,
        "login_progressive_base_delay_seconds": 1,
        "login_progressive_max_delay_seconds": 60,
        "login_rate_window_seconds": 60,
        "login_global_rate_limit": 600,
        "login_network_rate_limit": 30,
        "login_device_rate_limit": 20,
        "login_account_risk_window_seconds": 86_400,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(
    *,
    peer: str,
    forwarded_for: str | None = None,
    device_token: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    if device_token is not None:
        headers.append(
            (
                b"cookie",
                f"{DEVICE_COOKIE}={device_token}".encode("ascii"),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 43210),
            "server": ("admin.test", 443),
        }
    )


def test_trusted_proxy_can_supply_one_canonical_client_ip() -> None:
    request = _request(peer="172.20.0.4", forwarded_for="203.0.113.9")

    assert resolve_client_ip(request, _settings()) == "203.0.113.9"


@pytest.mark.parametrize(
    "peer, forwarded_for, expected",
    [
        ("198.51.100.5", "203.0.113.9", "198.51.100.5"),
        ("172.20.0.4", "203.0.113.9, 198.51.100.5", "172.20.0.4"),
        ("172.20.0.4", "not-an-ip", "172.20.0.4"),
    ],
)
def test_spoofed_or_ambiguous_forwarding_is_ignored(
    peer: str,
    forwarded_for: str,
    expected: str,
) -> None:
    request = _request(peer=peer, forwarded_for=forwarded_for)

    assert resolve_client_ip(request, _settings()) == expected


def test_login_source_uses_a_persistent_httponly_device_cookie() -> None:
    source = build_login_source(
        _request(peer="198.51.100.5"),
        "admin@example.com",
        _settings(),
    )
    response = Response()

    set_device_cookie(response, source, _settings())

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{DEVICE_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    assert "Path=/api/auth/login" in cookie

    repeated = build_login_source(
        _request(peer="198.51.100.5", device_token=source.device_token),
        "admin@example.com",
        _settings(),
    )
    assert repeated.network_hash == source.network_hash
    assert repeated.device_hash == source.device_hash
    assert not repeated.device_cookie_required


@pytest.mark.parametrize(
    "failure_count, expected",
    [(1, 0), (2, 0), (3, 1), (4, 2), (8, 32), (9, 60), (30, 60)],
)
def test_progressive_delay_is_source_scoped_and_capped(
    failure_count: int,
    expected: int,
) -> None:
    assert progressive_delay_seconds(failure_count, _settings()) == expected


class _PreflightConnection:
    def __init__(self) -> None:
        self.bucket_attempts: dict[tuple[str, str], int] = {}
        self.window_started_at: datetime | None = None
        self.next_allowed_at: dict[str, datetime] = {}

    async def execute(self, query: str, *args):
        assert "DELETE FROM admin_login_" in query
        return "DELETE 0"

    async def fetchrow(self, query: str, *args):
        assert "INSERT INTO admin_login_rate_buckets" in query
        scope, key_hash, _, now = args
        key = (scope, key_hash)
        self.bucket_attempts[key] = self.bucket_attempts.get(key, 0) + 1
        self.window_started_at = self.window_started_at or now
        return {
            "window_started_at": self.window_started_at,
            "attempt_count": self.bucket_attempts[key],
        }

    async def fetchval(self, query: str, *args):
        assert "FROM admin_login_source_risk" in query
        return self.next_allowed_at.get(str(args[0]))


@pytest.mark.asyncio
async def test_preflight_combines_global_network_device_and_source_limits() -> None:
    settings = _settings(
        login_global_rate_limit=100,
        login_network_rate_limit=100,
        login_device_rate_limit=1,
    )
    now = datetime.now(timezone.utc)
    source = build_login_source(
        _request(peer="198.51.100.5"),
        "admin@example.com",
        settings,
    )
    conn = _PreflightConnection()

    assert await preflight_login(conn, source, settings, now) == 0
    assert await preflight_login(conn, source, settings, now) == 60

    conn.next_allowed_at["network"] = now + timedelta(seconds=75)
    assert await preflight_login(conn, source, settings, now) == 75
