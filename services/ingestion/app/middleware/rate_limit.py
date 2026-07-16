"""Redis-backed event/byte token bucket shared by every Ingestion process."""

from __future__ import annotations

import json
import math

from fastapi import Request
from fastapi.responses import Response

from app.client_ip import client_ip

DEFAULT_CAPACITY = 1_000
DEFAULT_RATE = 100  # cost units per second

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_seconds = tonumber(ARGV[4])
local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
local state = redis.call('HMGET', key, 'tokens', 'updated_at_ms')
local tokens = tonumber(state[1]) or capacity
local updated_at_ms = tonumber(state[2]) or now_ms
local elapsed_ms = math.max(now_ms - updated_at_ms, 0)
tokens = math.min(capacity, tokens + elapsed_ms * refill_per_second / 1000)

if tokens < cost then
  redis.call('HSET', key, 'tokens', tokens, 'updated_at_ms', now_ms)
  redis.call('EXPIRE', key, ttl_seconds)
  local retry_after = math.ceil((cost - tokens) / refill_per_second)
  return {0, math.floor(tokens), math.max(retry_after, 1)}
end

tokens = tokens - cost
redis.call('HSET', key, 'tokens', tokens, 'updated_at_ms', now_ms)
redis.call('EXPIRE', key, ttl_seconds)
return {1, math.floor(tokens), 0}
"""


def request_cost(event_count: int, serialized_bytes: int) -> int:
    """Charge one unit per event plus one unit per KiB received."""
    return event_count + max(1, math.ceil(serialized_bytes / 1024))


async def check_rate_limit(
    redis,
    project_id: str,
    request: Request,
    *,
    cost: int,
) -> Response | None:
    """Return ``None`` when allowed, otherwise a 429/503 JSON response."""
    if project_id:
        bucket_key = f"apdl:rate:project:{project_id}"
    else:
        bucket_key = f"apdl:rate:ip:{client_ip(request)}"

    ttl_seconds = max(math.ceil(DEFAULT_CAPACITY / DEFAULT_RATE) * 2, 60)
    try:
        allowed, remaining, retry_after = await redis.eval(
            _TOKEN_BUCKET_LUA,
            1,
            bucket_key,
            DEFAULT_CAPACITY,
            DEFAULT_RATE,
            cost,
            ttl_seconds,
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
            "message": "Event and byte quota exceeded",
        },
        headers={
            "Retry-After": str(max(int(retry_after), 1)),
            "X-RateLimit-Limit": str(DEFAULT_CAPACITY),
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
