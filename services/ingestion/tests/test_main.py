import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from app import main


@pytest.mark.asyncio
async def test_credential_bearing_redis_url_is_redacted_from_startup_logs(
    caplog,
    monkeypatch,
):
    redis_url = (
        "rediss://redis-user:redis-password@cache.internal:6380/4"
        "?token=query-secret"
    )
    redis = SimpleNamespace(aclose=AsyncMock())
    pool = SimpleNamespace(close=AsyncMock())
    from_url = MagicMock(return_value=redis)
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setenv("REDIS_URL", redis_url)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://local.test/apdl")
    monkeypatch.setattr(main.aioredis, "from_url", from_url)
    monkeypatch.setattr(main.asyncpg, "create_pool", create_pool)

    with caplog.at_level(logging.INFO, logger="app.main"):
        async with main.lifespan(FastAPI()):
            pass

    log_output = "\n".join(caplog.messages)
    assert "cache.internal" in log_output
    assert "port=6380" in log_output
    assert "db=4" in log_output
    assert "tls=enabled" in log_output
    assert "redis-user" not in log_output
    assert "redis-password" not in log_output
    assert "query-secret" not in log_output
    from_url.assert_called_once_with(redis_url)
    redis.aclose.assert_awaited_once()
    pool.close.assert_awaited_once()
