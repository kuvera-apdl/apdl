"""The self-hosted worker validates its required env before touching the SDK."""

import pytest

from app.worker.environment_worker import run_worker


@pytest.mark.asyncio
async def test_worker_requires_environment_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ENVIRONMENT_KEY", raising=False)
    monkeypatch.setenv("CODEGEN_ENVIRONMENT_ID", "env_123")
    with pytest.raises(RuntimeError, match="ANTHROPIC_ENVIRONMENT_KEY"):
        await run_worker()


@pytest.mark.asyncio
async def test_worker_requires_environment_id(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_ENVIRONMENT_KEY", "sk-ant-oat01-x")
    monkeypatch.delenv("CODEGEN_ENVIRONMENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="CODEGEN_ENVIRONMENT_ID"):
        await run_worker()
