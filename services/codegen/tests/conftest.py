from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.main import app


@pytest.fixture(autouse=True)
def authorized_codegen_request() -> Iterable[
    Callable[[str, frozenset[str] | None], None]
]:
    """Keep existing endpoint tests focused while allowing explicit tenant tests."""

    def authorize(
        project_id: str = "demo", roles: frozenset[str] | None = None
    ) -> None:
        resolved_roles = roles or frozenset({"agents:read", "agents:manage"})

        async def authenticate(request: Request) -> Principal:
            principal = Principal(
                credential_id="test-credential",
                project_id=project_id,
                roles=resolved_roles,
                execution_authorized=True,
            )
            request.state.principal = principal
            return principal

        app.dependency_overrides[authenticate_request] = authenticate

    authorize()
    yield authorize
    app.dependency_overrides.pop(authenticate_request, None)
