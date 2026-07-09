import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.main import app


@pytest.fixture(autouse=True)
def authenticated_request_context():
    async def authenticate_test_request(request: Request):
        principal = Principal(
            credential_id="test-config",
            project_id="apdl",
            roles=frozenset(
                {
                    "config:read",
                    "config:write",
                    "config:evaluate",
                }
            ),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_test_request
    yield
    app.dependency_overrides.pop(authenticate_request, None)
