"""Version-aware Redis cache operations for flags and experiments."""

from __future__ import annotations

import json
from dataclasses import dataclass

FLAGS_PREFIX = "config:flags:v2:"
EXPERIMENTS_PREFIX = "config:experiments:"

_DECIMAL_LESS_LUA = """
local function decimal_less(left, right)
  left = string.gsub(tostring(left), '^0+', '')
  right = string.gsub(tostring(right), '^0+', '')
  if left == '' then left = '0' end
  if right == '' then right = '0' end
  if string.len(left) ~= string.len(right) then
    return string.len(left) < string.len(right)
  end
  return left < right
end
"""

_GET_FLAGS_LUA = _DECIMAL_LESS_LUA + """
local data = redis.call('GET', KEYS[1])
local cached_version = redis.call('GET', KEYS[2])
if not data or not cached_version then
  return nil
end
local watermark = redis.call('GET', KEYS[3]) or '0'
if decimal_less(cached_version, watermark) then
  redis.call('DEL', KEYS[1], KEYS[2])
  return nil
end
return {data, cached_version}
"""

_SET_FLAGS_LUA = _DECIMAL_LESS_LUA + """
local candidate = ARGV[1]
local watermark = redis.call('GET', KEYS[3]) or '0'
if decimal_less(candidate, watermark) then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
redis.call('SET', KEYS[2], candidate, 'EX', ARGV[3])
if decimal_less(watermark, candidate) then
  redis.call('SET', KEYS[3], candidate)
end
return 1
"""

_INVALIDATE_FLAGS_LUA = _DECIMAL_LESS_LUA + """
local incoming = ARGV[1]
local watermark = redis.call('GET', KEYS[3]) or '0'
if decimal_less(watermark, incoming) then
  redis.call('SET', KEYS[3], incoming)
end
local cached_version = redis.call('GET', KEYS[2])
if cached_version and not decimal_less(incoming, cached_version) then
  redis.call('DEL', KEYS[1], KEYS[2])
  return 1
end
return 0
"""


@dataclass(frozen=True)
class FlagCacheEntry:
    project_version: int
    config_json: str


def _flag_keys(project_id: str) -> tuple[str, str, str]:
    namespace = f"{FLAGS_PREFIX}{{{project_id}}}"
    return (
        f"{namespace}:data",
        f"{namespace}:data-version",
        f"{namespace}:watermark",
    )


def _project_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("project_version must be a non-negative integer")
    return value


async def get_flags(redis, project_id: str) -> FlagCacheEntry | None:
    """Return a cache entry only when it is at least the invalidation watermark."""
    keys = _flag_keys(project_id)
    result = await redis.eval(_GET_FLAGS_LUA, len(keys), *keys)
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        return None
    raw_entry, raw_version = result
    entry_json = (
        raw_entry.decode("utf-8") if isinstance(raw_entry, bytes) else raw_entry
    )
    version_text = (
        raw_version.decode("utf-8")
        if isinstance(raw_version, bytes)
        else str(raw_version)
    )
    try:
        entry = json.loads(entry_json)
        project_version = int(version_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        await redis.delete(keys[0], keys[1])
        return None
    if (
        not isinstance(entry, dict)
        or set(entry) != {"project_version", "config"}
        or isinstance(entry["project_version"], bool)
        or entry["project_version"] != project_version
        or project_version < 0
        or not isinstance(entry["config"], dict)
    ):
        await redis.delete(keys[0], keys[1])
        return None
    return FlagCacheEntry(
        project_version=project_version,
        config_json=json.dumps(entry["config"], separators=(",", ":")),
    )


async def set_flags(
    redis,
    project_id: str,
    project_version: int,
    data: str,
    ttl: int = 60,
) -> bool:
    """Atomically cache a snapshot unless a newer invalidation already won."""
    project_version = _project_version(project_version)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise ValueError("ttl must be a positive integer")
    config = json.loads(data)
    if not isinstance(config, dict):
        raise ValueError("flag cache data must be a JSON object")
    cache_entry = json.dumps(
        {"project_version": project_version, "config": config},
        separators=(",", ":"),
    )
    keys = _flag_keys(project_id)
    result = await redis.eval(
        _SET_FLAGS_LUA,
        len(keys),
        *keys,
        project_version,
        cache_entry,
        ttl,
    )
    return int(result) == 1


async def invalidate_flags(redis, project_id: str, project_version: int) -> None:
    """Advance the watermark and remove no snapshot newer than this event."""
    project_version = _project_version(project_version)
    keys = _flag_keys(project_id)
    await redis.eval(
        _INVALIDATE_FLAGS_LUA,
        len(keys),
        *keys,
        project_version,
    )


async def get_experiments(redis, project_id: str) -> str | None:
    """Return cached experiments JSON for a project, or None on miss."""
    val = await redis.get(EXPERIMENTS_PREFIX + project_id)
    if val is None:
        return None
    return val.decode("utf-8") if isinstance(val, bytes) else val


async def set_experiments(redis, project_id: str, data: str, ttl: int = 60) -> None:
    """Cache experiments JSON for a project with the given TTL (seconds)."""
    await redis.set(EXPERIMENTS_PREFIX + project_id, data, ex=ttl)


async def invalidate_experiments(redis, project_id: str) -> None:
    """Delete cached experiments for a project."""
    await redis.delete(EXPERIMENTS_PREFIX + project_id)
