from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest
from fastapi import Request

from app.auth import Principal, authenticate_request
from app.main import app

_TEST_GITHUB_WEBHOOK_SECRET = "test_" + "a" * 59


@pytest.fixture(autouse=True)
def authorized_codegen_request(monkeypatch: pytest.MonkeyPatch) -> Iterable[
    Callable[..., None]
]:
    """Keep existing endpoint tests focused while allowing explicit tenant tests."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _TEST_GITHUB_WEBHOOK_SECRET)

    def authorize(
        project_id: str = "demo",
        roles: frozenset[str] | None = None,
        *,
        auth_kind: str = "api_key",
        capability_run_id: str | None = None,
        actor_user_id: str | None = None,
        execution_authorized: bool = True,
    ) -> None:
        resolved_roles = roles or frozenset({"agents:read", "agents:manage"})

        async def authenticate(request: Request) -> Principal:
            principal = Principal(
                credential_id="test-credential",
                project_id=project_id,
                roles=resolved_roles,
                execution_authorized=execution_authorized,
                actor_user_id=actor_user_id,
                auth_kind=auth_kind,
                capability_run_id=capability_run_id,
            )
            request.state.principal = principal
            return principal

        app.dependency_overrides[authenticate_request] = authenticate

    authorize()
    yield authorize
    app.dependency_overrides.pop(authenticate_request, None)
