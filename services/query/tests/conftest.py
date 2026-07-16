import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.main import app


@pytest.fixture(autouse=True)
def authenticated_request_context():
    async def authenticate_test_request(request: Request):
        principal = Principal(
            credential_id="test-query",
            project_id="apiasport",
            roles=frozenset({"query:read"}),
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_test_request
    yield
    app.dependency_overrides.pop(authenticate_request, None)
