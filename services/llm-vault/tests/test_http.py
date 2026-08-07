"""HTTP authentication and secret-minimizing vault contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app import main as vault_main
from app.contracts import (
    CodegenProjection,
    ConnectionDetail,
    ConnectionList,
    CredentialAccessResponse,
)
from app.store import VaultAuthorizationError


ACTOR_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000002")
CREDENTIAL_ID = UUID("30000000-0000-4000-8000-000000000003")


def _detail() -> ConnectionDetail:
    now = datetime(2026, 7, 31, tzinfo=timezone.utc)
    return ConnectionDetail(
        connection_id=CONNECTION_ID,
        project_id="demo",
        provider="openai",
        label="Primary",
        version=1,
        inventory_version=1,
        state="active",
        consumers=("codegen",),
        validated_at=now,
        created_at=now,
        updated_at=now,
        revoked_at=None,
        model_count=1,
        models=(
            {
                "schema_version": "project_llm_provider_model@1",
                "model_id": "gpt-5.4-mini",
            },
        ),
    )


class _Store:
    def __init__(self) -> None:
        self.access_calls = []
        self.create_calls = []
        self.events: list[str] = []
        self.authority_error: VaultAuthorizationError | None = None

    async def list(self, project_id: str, actor_user_id: UUID) -> ConnectionList:
        assert project_id == "demo"
        assert actor_user_id == ACTOR_ID
        return ConnectionList(project_id="demo", connections=(_detail(),))

    async def issue_access(self, request):
        self.access_calls.append(request)
        return CredentialAccessResponse(
            access_id=UUID("40000000-0000-4000-8000-000000000004"),
            connection_id=CONNECTION_ID,
            credential_id=CREDENTIAL_ID,
            credential_version=1,
            provider="openai",
            api_key="provider-secret",
        )

    async def assert_create_authority(
        self, *, project_id: str, actor_user_id: UUID
    ) -> None:
        assert project_id == "demo"
        assert actor_user_id == ACTOR_ID
        self.events.append("authority")
        if self.authority_error is not None:
            raise self.authority_error

    async def create(self, **kwargs):
        self.events.append("create")
        self.create_calls.append(kwargs)
        return _detail()


@pytest.fixture
def configured_app() -> _Store:
    store = _Store()
    vault_main.app.state.settings = SimpleNamespace(
        admin_token="admin-token-" * 3,
        agents_token="agents-token-" * 3,
        codegen_token="codegen-token-" * 3,
    )
    vault_main.app.state.store = store
    vault_main.app.state.projector = object()
    return store


@pytest.mark.asyncio
async def test_admin_reads_require_token_actor_and_exact_project_assertion(
    configured_app: _Store,
) -> None:
    del configured_app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=vault_main.app),
        base_url="http://test",
    ) as client:
        denied = await client.get(
            "/v1/llm-connections",
            params={"project_id": "demo"},
            headers={
                "authorization": "Bearer " + "admin-token-" * 3,
                "x-apdl-project-id": "other",
                "x-apdl-actor-user-id": str(ACTOR_ID),
            },
        )
        allowed = await client.get(
            "/v1/llm-connections",
            params={"project_id": "demo"},
            headers={
                "authorization": "Bearer " + "admin-token-" * 3,
                "x-apdl-project-id": "demo",
                "x-apdl-actor-user-id": str(ACTOR_ID),
            },
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert "credential_id" not in allowed.text
    assert "api_key" not in allowed.text


@pytest.mark.asyncio
async def test_workload_token_is_bound_to_the_declared_consumer(
    configured_app: _Store,
) -> None:
    body = {
        "schema_version": "llm_credential_access_request@1",
        "project_id": "demo",
        "provider": "openai",
        "consumer": "codegen",
        "execution_id": "attempt-1",
        "purpose": "codegen.edit",
        "expected_credential_id": str(CREDENTIAL_ID),
        "expected_credential_version": 1,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=vault_main.app),
        base_url="http://test",
    ) as client:
        denied = await client.post(
            "/internal/v1/credential-access",
            headers={"authorization": "Bearer " + "agents-token-" * 3},
            json=body,
        )
        allowed = await client.post(
            "/internal/v1/credential-access",
            headers={"authorization": "Bearer " + "codegen-token-" * 3},
            json=body,
        )

    assert denied.status_code == 401
    assert len(configured_app.access_calls) == 1
    assert configured_app.access_calls[0].consumer == "codegen"
    assert allowed.status_code == 200
    assert allowed.json()["api_key"] == "provider-secret"


@pytest.mark.asyncio
async def test_non_ascii_authentication_headers_fail_closed(
    configured_app: _Store,
) -> None:
    access_body = {
        "schema_version": "llm_credential_access_request@1",
        "project_id": "demo",
        "provider": "openai",
        "consumer": "codegen",
        "execution_id": "attempt-non-ascii",
        "purpose": "codegen.edit",
        "expected_credential_id": str(CREDENTIAL_ID),
        "expected_credential_version": 1,
    }
    actor_header = str(ACTOR_ID).encode("ascii")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=vault_main.app),
        base_url="http://test",
    ) as client:
        invalid_admin_token = await client.get(
            "/v1/llm-connections",
            params={"project_id": "demo"},
            headers=[
                (b"authorization", b"Bearer \xff"),
                (b"x-apdl-project-id", b"demo"),
                (b"x-apdl-actor-user-id", actor_header),
            ],
        )
        invalid_project = await client.get(
            "/v1/llm-connections",
            params={"project_id": "demo"},
            headers=[
                (b"authorization", b"Bearer " + (b"admin-token-" * 3)),
                (b"x-apdl-project-id", b"\xff"),
                (b"x-apdl-actor-user-id", actor_header),
            ],
        )
        invalid_consumer_token = await client.post(
            "/internal/v1/credential-access",
            headers=[(b"authorization", b"Bearer \xff")],
            json=access_body,
        )

    assert invalid_admin_token.status_code == 401
    assert invalid_project.status_code == 403
    assert invalid_consumer_token.status_code == 401
    assert configured_app.access_calls == []


@pytest.mark.asyncio
async def test_create_validates_authority_before_egress_and_does_not_echo_key(
    configured_app: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def discover(provider: str, api_key: str) -> tuple[str, ...]:
        assert (provider, api_key) == ("openai", "provider-secret")
        configured_app.events.append("discovery")
        return ("gpt-5.4-mini",)

    async def project(*_args, **_kwargs):
        configured_app.events.append("projection")
        return {
            "codegen": CodegenProjection(
                schema_version="codegen_llm_model_projection@1",
                catalog_version="codegen-provider-catalog@1",
                models=(),
            )
        }

    monkeypatch.setattr(vault_main, "discover_model_ids", discover)
    monkeypatch.setattr(vault_main, "_project_models", project)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=vault_main.app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/llm-connections",
            headers={
                "authorization": "Bearer " + "admin-token-" * 3,
                "x-apdl-project-id": "demo",
                "x-apdl-actor-user-id": str(ACTOR_ID),
            },
            json={
                "project_id": "demo",
                "provider": "openai",
                "label": "Primary",
                "api_key": "provider-secret",
                "consumers": ["codegen"],
            },
        )

    assert response.status_code == 201, response.text
    assert "provider-secret" not in response.text
    assert configured_app.events == [
        "authority",
        "discovery",
        "projection",
        "create",
    ]
    assert configured_app.create_calls[0]["api_key"] == "provider-secret"


@pytest.mark.asyncio
async def test_create_denies_before_provider_egress(
    configured_app: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_app.authority_error = VaultAuthorizationError(
        "Project credential authority is unavailable"
    )

    async def unexpected_egress(*_args, **_kwargs):
        configured_app.events.append("unexpected-egress")
        raise AssertionError("provider egress must not run without authority")

    monkeypatch.setattr(vault_main, "discover_model_ids", unexpected_egress)
    monkeypatch.setattr(vault_main, "_project_models", unexpected_egress)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=vault_main.app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/llm-connections",
            headers={
                "authorization": "Bearer " + "admin-token-" * 3,
                "x-apdl-project-id": "demo",
                "x-apdl-actor-user-id": str(ACTOR_ID),
            },
            json={
                "project_id": "demo",
                "provider": "openai",
                "label": "Primary",
                "api_key": "provider-secret",
                "consumers": ["codegen"],
            },
        )

    assert response.status_code == 403
    assert configured_app.events == ["authority"]
    assert configured_app.create_calls == []
