import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.main import app


@pytest.fixture(autouse=True)
def authenticated_request_context(monkeypatch):
    monkeypatch.setenv(
        "APDL_SERVICE_API_KEYS",
        '{"apdl":"proj_apdl_0123456789abcdef0123456789abcdef",'
        '"demo":"proj_demo_0123456789abcdef0123456789abcdef"}',
    )

    async def authenticate_test_request(request: Request):
        principal = Principal(
            credential_id="test-agents",
            project_id="demo",
            roles=frozenset(
                {
                    "agents:read",
                    "agents:run",
                    "agents:manage",
                    "agents:approve",
                }
            ),
            self_registered_project=False,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_test_request
    yield
    app.dependency_overrides.pop(authenticate_request, None)
