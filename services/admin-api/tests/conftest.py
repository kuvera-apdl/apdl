from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import proxy
from app.auth import AdminSession, require_session
from app.config import Settings

TEST_API_KEY = "proj_demo_0123456789abcdef"


def make_settings(**overrides) -> Settings:
    values = {
        "postgres_url": "postgresql://test",
        "service_urls": {
            "ingestion": "http://ingestion.test",
            "config": "http://config.test",
            "query": "http://query.test",
            "agents": "http://agents.test",
            "codegen": "http://codegen.test",
        },
        "service_api_keys": {"demo": TEST_API_KEY},
        "internal_token": "internal-test-token",
        "allowed_origins": frozenset({"http://admin.test"}),
        "cookie_secure": False,
        "session_ttl_seconds": 28_800,
        "session_idle_seconds": 1_800,
        "login_failure_limit": 5,
        "login_lock_seconds": 900,
        "max_request_bytes": 2_097_152,
    }
    values.update(overrides)
    return Settings(**values)


class AuditConnection:
    def __init__(self, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        self.statements = statements

    async def execute(self, query: str, *args):
        self.statements.append((query, args))
        return "OK"


class AuditPool:
    def __init__(self, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        self.connection = AuditConnection(statements)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
def admin_session() -> AdminSession:
    return AdminSession(
        session_id="10000000-0000-4000-8000-000000000001",
        token_hash="a" * 64,
        csrf_hash="b" * 64,
        user_id="20000000-0000-4000-8000-000000000002",
        email="admin@example.com",
        projects={
            "demo": frozenset(
                {
                    "events:write",
                    "config:read",
                    "config:write",
                    "config:evaluate",
                    "query:read",
                    "agents:read",
                    "agents:run",
                    "agents:manage",
                    "agents:approve",
                }
            )
        },
    )


@asynccontextmanager
async def proxy_client(
    transport: httpx.AsyncBaseTransport,
    session: AdminSession,
    settings: Settings | None = None,
) -> AsyncIterator[TestClient]:
    app = FastAPI()
    app.state.settings = settings or make_settings()
    app.state.http_client = httpx.AsyncClient(transport=transport)
    app.state.audit_statements = []
    app.state.pg_pool = AuditPool(app.state.audit_statements)
    app.include_router(proxy.router)
    app.dependency_overrides[require_session] = lambda: session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        await app.state.http_client.aclose()
