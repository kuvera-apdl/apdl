"""Source-scoped login throttling and durable account-risk signals."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import Request, Response

from app.security import new_token

if TYPE_CHECKING:
    from app.config import Settings

DEVICE_COOKIE = "apdl_admin_device"
DEVICE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
GENERIC_LOGIN_ERROR = "Invalid email or password"
THROTTLED_LOGIN_ERROR = "Too many attempts. Try again later."


@dataclass(frozen=True)
class LoginSource:
    email_hash: str
    global_hash: str
    network_hash: str
    device_hash: str
    device_token: str
    device_cookie_required: bool


def _risk_hash(secret: str, label: str, *values: str) -> str:
    message = "\x1f".join((label, *values)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def resolve_client_ip(request: Request, settings: Settings) -> str:
    """Resolve one canonical client IP without trusting caller-supplied chains."""

    peer_raw = request.client.host if request.client is not None else ""
    try:
        peer = ipaddress.ip_address(peer_raw)
    except ValueError:
        return "unknown"

    forwarded = request.headers.get("x-forwarded-for", "").strip()
    peer_is_trusted = any(peer in network for network in settings.trusted_proxy_cidrs)
    if peer_is_trusted and forwarded and "," not in forwarded:
        try:
            return ipaddress.ip_address(forwarded).compressed
        except ValueError:
            pass
    return peer.compressed


def build_login_source(
    request: Request,
    email: str,
    settings: Settings,
) -> LoginSource:
    raw_device = request.cookies.get(DEVICE_COOKIE, "")
    device_cookie_required = DEVICE_TOKEN_PATTERN.fullmatch(raw_device) is None
    device_token = new_token() if device_cookie_required else raw_device
    client_ip = resolve_client_ip(request, settings)
    secret = settings.login_risk_hmac_key
    email_hash = _risk_hash(secret, "email", email)
    network_hash = _risk_hash(secret, "network", client_ip)
    device_hash = _risk_hash(secret, "device", device_token)
    return LoginSource(
        email_hash=email_hash,
        global_hash=_risk_hash(secret, "global", "admin-login"),
        network_hash=network_hash,
        device_hash=device_hash,
        device_token=device_token,
        device_cookie_required=device_cookie_required,
    )


def set_device_cookie(
    response: Response,
    source: LoginSource,
    settings: Settings,
) -> None:
    if not source.device_cookie_required:
        return
    response.set_cookie(
        DEVICE_COOKIE,
        source.device_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        max_age=settings.login_device_ttl_seconds,
        path="/api/auth/login",
    )


def progressive_delay_seconds(failure_count: int, settings: Settings) -> int:
    if failure_count < settings.login_progressive_failure_threshold:
        return 0
    exponent = failure_count - settings.login_progressive_failure_threshold
    return min(
        settings.login_progressive_max_delay_seconds,
        settings.login_progressive_base_delay_seconds * (2**exponent),
    )


async def _consume_rate_bucket(
    conn,
    *,
    scope: str,
    key_hash: str,
    limit: int,
    settings: Settings,
    now: datetime,
) -> int:
    row = await conn.fetchrow(
        """
        INSERT INTO admin_login_rate_buckets (
            scope, key_hash, window_started_at, attempt_count, updated_at
        ) VALUES ($1, $2, $4, 1, $4)
        ON CONFLICT (scope, key_hash) DO UPDATE
        SET window_started_at = CASE
                WHEN admin_login_rate_buckets.window_started_at
                     <= $4 - ($3 * INTERVAL '1 second')
                THEN $4
                ELSE admin_login_rate_buckets.window_started_at
            END,
            attempt_count = CASE
                WHEN admin_login_rate_buckets.window_started_at
                     <= $4 - ($3 * INTERVAL '1 second')
                THEN 1
                ELSE admin_login_rate_buckets.attempt_count + 1
            END,
            updated_at = $4
        RETURNING window_started_at, attempt_count
        """,
        scope,
        key_hash,
        settings.login_rate_window_seconds,
        now,
    )
    if int(row["attempt_count"]) <= limit:
        return 0
    retry_at = row["window_started_at"] + timedelta(
        seconds=settings.login_rate_window_seconds
    )
    return max(1, math.ceil((retry_at - now).total_seconds()))


async def preflight_login(
    conn,
    source: LoginSource,
    settings: Settings,
    now: datetime,
) -> int:
    await conn.execute(
        """
        DELETE FROM admin_login_rate_buckets
        WHERE updated_at < $1 - ($2 * INTERVAL '1 second')
        """,
        now,
        settings.login_rate_window_seconds * 2,
    )
    await conn.execute(
        """
        DELETE FROM admin_login_source_risk
        WHERE updated_at < $1 - ($2 * INTERVAL '1 second')
        """,
        now,
        settings.login_account_risk_window_seconds * 2,
    )

    retry_after = 0
    for scope, key_hash, limit in (
        ("global", source.global_hash, settings.login_global_rate_limit),
        ("network", source.network_hash, settings.login_network_rate_limit),
        ("device", source.device_hash, settings.login_device_rate_limit),
    ):
        retry_after = max(
            retry_after,
            await _consume_rate_bucket(
                conn,
                scope=scope,
                key_hash=key_hash,
                limit=limit,
                settings=settings,
                now=now,
            ),
        )

    for scope, source_hash in (
        ("network", source.network_hash),
        ("device", source.device_hash),
    ):
        next_allowed_at = await conn.fetchval(
            """
            SELECT next_allowed_at
            FROM admin_login_source_risk
            WHERE scope = $1
              AND source_hash = $2
              AND email_hash = $3
            """,
            scope,
            source_hash,
            source.email_hash,
        )
        if next_allowed_at is not None and next_allowed_at > now:
            retry_after = max(
                retry_after,
                max(1, math.ceil((next_allowed_at - now).total_seconds())),
            )
    return retry_after


async def record_failed_login(
    conn,
    *,
    source: LoginSource,
    user_id: uuid.UUID | None,
    settings: Settings,
    now: datetime,
) -> int:
    delay = 0
    for scope, source_hash in (
        ("network", source.network_hash),
        ("device", source.device_hash),
    ):
        failure_count = int(
            await conn.fetchval(
                """
                INSERT INTO admin_login_source_risk (
                    scope,
                    source_hash,
                    email_hash,
                    failure_count,
                    next_allowed_at,
                    last_failed_at,
                    updated_at
                ) VALUES ($1, $2, $3, 1, $4, $4, $4)
                ON CONFLICT (scope, source_hash, email_hash) DO UPDATE
                SET failure_count = admin_login_source_risk.failure_count + 1,
                    last_failed_at = $4,
                    updated_at = $4
                RETURNING failure_count
                """,
                scope,
                source_hash,
                source.email_hash,
                now,
            )
        )
        source_delay = progressive_delay_seconds(failure_count, settings)
        delay = max(delay, source_delay)
        if source_delay > 0:
            await conn.execute(
                """
                UPDATE admin_login_source_risk
                SET next_allowed_at = GREATEST(
                        next_allowed_at,
                        $4 + ($5 * INTERVAL '1 second')
                    ),
                    updated_at = $4
                WHERE scope = $1
                  AND source_hash = $2
                  AND email_hash = $3
                """,
                scope,
                source_hash,
                source.email_hash,
                now,
                source_delay,
            )

    if user_id is not None:
        account_row = await conn.fetchrow(
            """
            INSERT INTO admin_login_account_risk (
                user_id,
                email_hash,
                window_started_at,
                failure_count,
                last_failed_at,
                updated_at
            ) VALUES ($1, $2, $3, 1, $3, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET email_hash = EXCLUDED.email_hash,
                window_started_at = CASE
                    WHEN admin_login_account_risk.window_started_at
                         <= $3 - ($4 * INTERVAL '1 second')
                    THEN $3
                    ELSE admin_login_account_risk.window_started_at
                END,
                failure_count = CASE
                    WHEN admin_login_account_risk.window_started_at
                         <= $3 - ($4 * INTERVAL '1 second')
                    THEN 1
                    ELSE admin_login_account_risk.failure_count + 1
                END,
                last_failed_at = $3,
                updated_at = $3
            RETURNING window_started_at, failure_count
            """,
            user_id,
            source.email_hash,
            now,
            settings.login_account_risk_window_seconds,
        )
        account_failures = int(account_row["failure_count"])
        if account_failures >= settings.login_account_notice_threshold:
            updated = await conn.execute(
                """
                UPDATE admin_security_notifications
                SET observed_failures = GREATEST(observed_failures, $2),
                    window_started_at = $3,
                    last_detected_at = $4
                WHERE user_id = $1
                  AND kind = 'suspicious_login_activity'
                  AND status = 'unread'
                """,
                user_id,
                account_failures,
                account_row["window_started_at"],
                now,
            )
            if updated == "UPDATE 0":
                await conn.execute(
                    """
                    INSERT INTO admin_security_notifications (
                        notification_id,
                        user_id,
                        kind,
                        observed_failures,
                        window_started_at,
                        last_detected_at
                    ) VALUES (
                        $1,
                        $2,
                        'suspicious_login_activity',
                        $3,
                        $4,
                        $5
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    uuid.uuid4(),
                    user_id,
                    account_failures,
                    account_row["window_started_at"],
                    now,
                )
    return delay


async def clear_login_source_risk(conn, source: LoginSource) -> None:
    await conn.execute(
        """
        DELETE FROM admin_login_source_risk
        WHERE email_hash = $1
          AND (
              (scope = 'network' AND source_hash = $2)
              OR (scope = 'device' AND source_hash = $3)
          )
        """,
        source.email_hash,
        source.network_hash,
        source.device_hash,
    )
