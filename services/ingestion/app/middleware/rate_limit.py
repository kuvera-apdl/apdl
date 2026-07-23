"""Atomic hierarchical quotas shared by every Ingestion process."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

from fastapi import Request
from fastapi.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import CredentialKind, Principal
from app.client_ip import client_ip


@dataclass(frozen=True)
class BucketLimit:
    capacity: int
    refill_per_second: int


@dataclass(frozen=True)
class BucketDebit:
    key: str
    limit: BucketLimit
    cost: int


# Request admission is deliberately independent from byte and event admission.
# A valid credential therefore pays for malformed and oversized requests before
# the application spends CPU or memory parsing them.
GLOBAL_REQUEST_LIMIT = BucketLimit(100_000, 10_000)
PROJECT_REQUEST_LIMIT = BucketLimit(1_000, 100)
CONFIDENTIAL_REQUEST_LIMIT = BucketLimit(1_000, 100)
BROWSER_REQUEST_LIMIT = BucketLimit(200, 20)
IP_REQUEST_LIMIT = BucketLimit(500, 50)

# Byte admission retains proportional protection for bodies that pass the hard
# 512 KiB request bound. A fresh browser bucket can admit exactly one maximum
# request while remaining subordinate to the project bucket.
GLOBAL_BYTE_LIMIT = BucketLimit(100_000, 10_000)
PROJECT_BYTE_LIMIT = BucketLimit(1_000, 100)
CONFIDENTIAL_BYTE_LIMIT = BucketLimit(1_000, 100)
BROWSER_BYTE_LIMIT = BucketLimit(512, 20)
IP_BYTE_LIMIT = BucketLimit(1_000, 50)

# The project event ceiling preserves the original 1,000 capacity / 100 events
# per-second contract. Browser credentials receive a subordinate 20% share so
# a public credential cannot consume the tenant ceiling by itself.
GLOBAL_EVENT_LIMIT = BucketLimit(100_000, 10_000)
PROJECT_EVENT_LIMIT = BucketLimit(1_000, 100)
CONFIDENTIAL_EVENT_LIMIT = BucketLimit(1_000, 100)
BROWSER_EVENT_LIMIT = BucketLimit(200, 20)
IP_EVENT_LIMIT = BucketLimit(500, 50)
IDENTITY_EVENT_LIMIT = BucketLimit(200, 20)

# Unauthenticated traffic must be bounded before an API key can cause a
# PostgreSQL lookup. These limits are intentionally independent from the
# authenticated request quota: a valid request pays both admission stages.
PREAUTH_GLOBAL_REQUEST_LIMIT = BucketLimit(2_000, 200)
PREAUTH_IP_REQUEST_LIMIT = BucketLimit(100, 10)


_HIERARCHICAL_TOKEN_BUCKET_LUA = """
local bucket_count = #KEYS
local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
local candidates = {}
local rejected_limit = 0
local rejected_remaining = 0
local rejected_retry_after = 0

-- First calculate every bucket without mutating Redis. If any child is full,
-- no parent or sibling is charged for the rejected request.
for index = 1, bucket_count do
  local offset = (index - 1) * 4
  local capacity = tonumber(ARGV[offset + 1])
  local refill_per_second = tonumber(ARGV[offset + 2])
  local cost = tonumber(ARGV[offset + 3])
  local ttl_seconds = tonumber(ARGV[offset + 4])
  local state = redis.call('HMGET', KEYS[index], 'tokens', 'updated_at_ms')
  local tokens = tonumber(state[1]) or capacity
  local updated_at_ms = tonumber(state[2]) or now_ms
  local elapsed_ms = math.max(now_ms - updated_at_ms, 0)
  tokens = math.min(capacity, tokens + elapsed_ms * refill_per_second / 1000)
  candidates[index] = {tokens, cost, ttl_seconds, capacity}

  if tokens < cost then
    local retry_after = math.max(
      math.ceil((cost - tokens) / refill_per_second),
      1
    )
    if retry_after > rejected_retry_after then
      rejected_limit = capacity
      rejected_remaining = math.floor(tokens)
      rejected_retry_after = retry_after
    end
  end
end

if rejected_retry_after > 0 then
  return {0, rejected_limit, rejected_remaining, rejected_retry_after}
end

local minimum_remaining = nil
local minimum_limit = 0
for index = 1, bucket_count do
  local candidate = candidates[index]
  local tokens = candidate[1] - candidate[2]
  redis.call('HSET', KEYS[index], 'tokens', tokens, 'updated_at_ms', now_ms)
  redis.call('EXPIRE', KEYS[index], candidate[3])
  local remaining = math.floor(tokens)
  if minimum_remaining == nil or remaining < minimum_remaining then
    minimum_remaining = remaining
    minimum_limit = candidate[4]
  end
end

return {1, minimum_limit, minimum_remaining or 0, 0}
"""


def _hash_bucket_identifier(kind: str, value: str) -> str:
    """Return an opaque, fixed-width Redis-key component."""
    return hashlib.sha256(f"{kind}\0{value}".encode("utf-8")).hexdigest()


def _ttl_seconds(limit: BucketLimit) -> int:
    return max(math.ceil(limit.capacity / limit.refill_per_second) * 2, 60)


def _credential_limit(
    principal: Principal,
    *,
    confidential: BucketLimit,
    browser: BucketLimit,
) -> BucketLimit:
    if principal.credential_kind is CredentialKind.CONFIDENTIAL:
        return confidential
    return browser


def _common_buckets(
    stage: str,
    principal: Principal,
    request: Request,
    *,
    cost: int,
    global_limit: BucketLimit,
    project_limit: BucketLimit,
    confidential_limit: BucketLimit,
    browser_limit: BucketLimit,
    ip_limit: BucketLimit,
) -> list[BucketDebit]:
    credential_hash = _hash_bucket_identifier(
        "credential", principal.credential_id
    )
    ip_hash = _hash_bucket_identifier("ip", client_ip(request))
    return [
        BucketDebit(f"apdl:rate:{stage}:global", global_limit, cost),
        BucketDebit(
            f"apdl:rate:{stage}:project:{principal.project_id}",
            project_limit,
            cost,
        ),
        BucketDebit(
            f"apdl:rate:{stage}:credential:{credential_hash}",
            _credential_limit(
                principal,
                confidential=confidential_limit,
                browser=browser_limit,
            ),
            cost,
        ),
        BucketDebit(
            f"apdl:rate:{stage}:ip:{ip_hash}",
            ip_limit,
            cost,
        ),
    ]


def _identity_costs(
    project_id: str,
    events: Sequence[Mapping[str, object]],
) -> Counter[str]:
    costs: Counter[str] = Counter()
    for event in events:
        anonymous_id = event.get("anonymous_id")
        if isinstance(anonymous_id, str):
            identity = f"anonymous_id\0{anonymous_id}"
        else:
            # Strict validation guarantees a non-empty user_id at this point.
            user_id = event["user_id"]
            identity = f"user_id\0{user_id}"
        tenant_identity = f"{project_id}\0{identity}"
        costs[_hash_bucket_identifier("identity", tenant_identity)] += 1
    return costs


def byte_cost(serialized_bytes: int) -> int:
    """Charge at least one unit, then one unit per started KiB."""
    return max(1, math.ceil(serialized_bytes / 1024))


async def admit_pre_auth_request(redis, request: Request) -> Response | None:
    """Bound event-ingestion traffic before any credential-registry lookup."""
    ip_hash = _hash_bucket_identifier("ip", client_ip(request))
    buckets = [
        BucketDebit(
            "apdl:rate:preauth:global",
            PREAUTH_GLOBAL_REQUEST_LIMIT,
            1,
        ),
        BucketDebit(
            f"apdl:rate:preauth:ip:{ip_hash}",
            PREAUTH_IP_REQUEST_LIMIT,
            1,
        ),
    ]
    return await _admit(
        redis,
        buckets,
        quota_name="Pre-authentication request",
    )


class PreAuthRateLimitMiddleware:
    """Apply Redis admission only to the exact event-ingestion operation."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != "/v1/events"
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        redis = getattr(request.app.state, "redis", None)
        response = await admit_pre_auth_request(redis, request)
        if response is not None:
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


async def admit_request(
    redis,
    principal: Principal,
    request: Request,
) -> Response | None:
    """Charge one authenticated request before inspecting its body."""
    buckets = _common_buckets(
        "request",
        principal,
        request,
        cost=1,
        global_limit=GLOBAL_REQUEST_LIMIT,
        project_limit=PROJECT_REQUEST_LIMIT,
        confidential_limit=CONFIDENTIAL_REQUEST_LIMIT,
        browser_limit=BROWSER_REQUEST_LIMIT,
        ip_limit=IP_REQUEST_LIMIT,
    )
    return await _admit(redis, buckets, quota_name="Request")


async def admit_bytes(
    redis,
    principal: Principal,
    request: Request,
    serialized_bytes: int,
) -> Response | None:
    """Charge a bounded body before JSON parsing, privacy, or schema work."""
    buckets = _common_buckets(
        "byte",
        principal,
        request,
        cost=byte_cost(serialized_bytes),
        global_limit=GLOBAL_BYTE_LIMIT,
        project_limit=PROJECT_BYTE_LIMIT,
        confidential_limit=CONFIDENTIAL_BYTE_LIMIT,
        browser_limit=BROWSER_BYTE_LIMIT,
        ip_limit=IP_BYTE_LIMIT,
    )
    return await _admit(redis, buckets, quota_name="Byte")


async def admit_events(
    redis,
    principal: Principal,
    request: Request,
    events: Sequence[Mapping[str, object]],
) -> Response | None:
    """Atomically charge the batch and all of its aggregated identities."""
    event_count = len(events)
    buckets = _common_buckets(
        "event",
        principal,
        request,
        cost=event_count,
        global_limit=GLOBAL_EVENT_LIMIT,
        project_limit=PROJECT_EVENT_LIMIT,
        confidential_limit=CONFIDENTIAL_EVENT_LIMIT,
        browser_limit=BROWSER_EVENT_LIMIT,
        ip_limit=IP_EVENT_LIMIT,
    )
    buckets.extend(
        BucketDebit(
            f"apdl:rate:event:identity:{identity_hash}",
            IDENTITY_EVENT_LIMIT,
            cost,
        )
        for identity_hash, cost in sorted(
            _identity_costs(principal.project_id, events).items()
        )
    )
    return await _admit(redis, buckets, quota_name="Event")


async def _admit(
    redis,
    buckets: Sequence[BucketDebit],
    *,
    quota_name: str,
) -> Response | None:
    """Return ``None`` when every bucket is debited, else a 429/503."""
    keys = [bucket.key for bucket in buckets]
    arguments: list[int] = []
    for bucket in buckets:
        arguments.extend(
            (
                bucket.limit.capacity,
                bucket.limit.refill_per_second,
                bucket.cost,
                _ttl_seconds(bucket.limit),
            )
        )
    try:
        allowed, limit, remaining, retry_after = await redis.eval(
            _HIERARCHICAL_TOKEN_BUCKET_LUA,
            len(keys),
            *keys,
            *arguments,
        )
    except Exception:
        return _json_response(
            503,
            {
                "error": "service_unavailable",
                "message": "Rate-limit authority is unavailable",
            },
        )

    if int(allowed) == 1:
        return None

    return _json_response(
        429,
        {
            "error": "rate_limited",
            "message": f"{quota_name} quota exceeded",
        },
        headers={
            "Retry-After": str(max(int(retry_after), 1)),
            "X-RateLimit-Limit": str(max(int(limit), 0)),
            "X-RateLimit-Remaining": str(max(int(remaining), 0)),
        },
    )


def _json_response(
    status_code: int,
    content: dict,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    return Response(
        content=json.dumps(content),
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )
