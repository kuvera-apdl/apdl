"""Versioned flag-cache watermark and compare-and-set contracts."""

import json

import pytest

from app.store import redis_cache


class SemanticRedis:
    """Small semantic fake for the three atomic Lua cache operations."""

    def __init__(self):
        self.values = {}

    async def eval(self, script, key_count, *values):
        keys = values[:key_count]
        args = values[key_count:]
        data_key, version_key, watermark_key = keys
        if script == redis_cache._GET_FLAGS_LUA:
            data = self.values.get(data_key)
            version = self.values.get(version_key)
            if data is None or version is None:
                return None
            watermark = int(self.values.get(watermark_key, 0))
            if int(version) < watermark:
                self.values.pop(data_key, None)
                self.values.pop(version_key, None)
                return None
            return [data, str(version)]
        if script == redis_cache._SET_FLAGS_LUA:
            candidate, entry, _ttl = args
            watermark = int(self.values.get(watermark_key, 0))
            if int(candidate) < watermark:
                return 0
            self.values[data_key] = entry
            self.values[version_key] = str(candidate)
            self.values[watermark_key] = str(max(watermark, int(candidate)))
            return 1
        if script == redis_cache._INVALIDATE_FLAGS_LUA:
            incoming = int(args[0])
            watermark = int(self.values.get(watermark_key, 0))
            self.values[watermark_key] = str(max(watermark, incoming))
            cached_version = self.values.get(version_key)
            if cached_version is not None and int(cached_version) <= incoming:
                self.values.pop(data_key, None)
                self.values.pop(version_key, None)
                return 1
            return 0
        raise AssertionError("unexpected Lua script")

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


def config_json(version: int) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "project_id": "apdl",
            "flags": [{"version": version}],
        },
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_older_population_cannot_cross_newer_invalidation_watermark():
    redis = SemanticRedis()

    await redis_cache.invalidate_flags(redis, "apdl", 11)
    stored = await redis_cache.set_flags(redis, "apdl", 10, config_json(10))

    assert stored is False
    assert await redis_cache.get_flags(redis, "apdl") is None


@pytest.mark.asyncio
async def test_older_invalidation_does_not_evict_newer_snapshot():
    redis = SemanticRedis()
    assert await redis_cache.set_flags(redis, "apdl", 12, config_json(12))

    await redis_cache.invalidate_flags(redis, "apdl", 11)

    entry = await redis_cache.get_flags(redis, "apdl")
    assert entry is not None
    assert entry.project_version == 12
    assert json.loads(entry.config_json)["flags"][0]["version"] == 12


@pytest.mark.asyncio
async def test_equal_or_newer_invalidation_removes_cached_snapshot():
    redis = SemanticRedis()
    assert await redis_cache.set_flags(redis, "apdl", 12, config_json(12))

    await redis_cache.invalidate_flags(redis, "apdl", 12)

    assert await redis_cache.get_flags(redis, "apdl") is None


def test_lua_compares_decimal_strings_without_float_precision_loss():
    for script in (
        redis_cache._GET_FLAGS_LUA,
        redis_cache._SET_FLAGS_LUA,
        redis_cache._INVALIDATE_FLAGS_LUA,
    ):
        assert "decimal_less" in script
        assert "tonumber" not in script
