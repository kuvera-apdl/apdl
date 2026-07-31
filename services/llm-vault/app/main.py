"""APDL project LLM credential vault service."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse

from app.auth import require_admin, require_consumer
from app.config import Settings
from app.contracts import (
    ConnectionDetail,
    ConnectionList,
    ConnectionSummary,
    Consumer,
    CreateConnectionRequest,
    CredentialAccessRequest,
    CredentialAccessResponse,
    Provider,
    RefreshConnectionRequest,
    ReplaceConnectionRequest,
    RevokeConnectionRequest,
)
from app.crypto import CredentialCipher
from app.discovery import ProviderDiscoveryError, discover_model_ids
from app.projections import (
    ModelProjector,
    ProjectionUnavailableError,
)
from app.store import (
    ProjectLlmVaultStore,
    Projection,
    VaultAuthorizationError,
    VaultConflictError,
    VaultNotFoundError,
    VaultStoreError,
)


logger = logging.getLogger(__name__)
PROJECT_PATTERN = r"^[A-Za-z0-9]{1,64}$"
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


async def _acquire_maintenance_inhibitor(connection: asyncpg.Connection) -> None:
    await connection.execute(
        "SELECT pg_advisory_lock_shared($1)",
        MAINTENANCE_INHIBITOR_LOCK_ID,
    )
    await connection.execute(
        "SELECT pg_advisory_lock_shared($1)",
        MAINTENANCE_GUARD_LOCK_ID,
    )


async def _reset_maintenance_inhibitor(connection: asyncpg.Connection) -> None:
    reset_query = connection.get_reset_query()
    if reset_query:
        await connection.execute(reset_query)
    await _acquire_maintenance_inhibitor(connection)


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = Settings.from_environment()
    cipher = CredentialCipher.from_base64(settings.encryption_key_base64)
    pool = await asyncpg.create_pool(
        settings.postgres_url,
        min_size=2,
        max_size=10,
        init=_acquire_maintenance_inhibitor,
        reset=_reset_maintenance_inhibitor,
        max_inactive_connection_lifetime=0,
    )
    client = httpx.AsyncClient()
    try:
        application.state.settings = settings
        application.state.pg_pool = pool
        application.state.store = ProjectLlmVaultStore(pool, cipher)
        application.state.projector = ModelProjector(
            client,
            agents_url=settings.agents_service_url,
            codegen_url=settings.codegen_service_url,
            token=settings.projection_token,
        )
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1 FROM llm_vault_connections LIMIT 1")
            key_mismatch = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM llm_vault_provider_credentials AS credential
                    LEFT JOIN llm_vault_provider_secrets AS secret
                      ON secret.credential_id = credential.credential_id
                    WHERE credential.state = 'active'
                      AND (
                          secret.credential_id IS NULL
                          OR secret.encryption_key_id <> $1
                      )
                )
                """,
                cipher.key_id,
            )
            if key_mismatch:
                raise RuntimeError(
                    "Configured vault key does not match active credential ciphertext"
                )
        yield
    finally:
        await client.aclose()
        await pool.close()


app = FastAPI(
    title="APDL Project LLM Credential Vault",
    version="0.1.0",
    lifespan=lifespan,
)


def _store(request: Request) -> ProjectLlmVaultStore:
    return request.app.state.store


def _projector(request: Request) -> ModelProjector:
    return request.app.state.projector


def _store_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VaultAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, VaultNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, VaultConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    logger.error("Vault storage operation failed", exc_info=exc)
    return HTTPException(
        status_code=503,
        detail="Project LLM credential vault is unavailable",
    )


def _discovery_error(exc: ProviderDiscoveryError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _project_models(
    projector: ModelProjector,
    consumers: tuple[Consumer, ...],
    provider: Provider,
    model_ids: tuple[str, ...],
) -> dict[Consumer, Projection]:
    values = await asyncio.gather(
        *(
            projector.project(consumer, provider, model_ids)
            for consumer in consumers
        )
    )
    return dict(zip(consumers, values, strict=True))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "apdl-llm-vault"}


@app.get("/ready")
async def ready(request: Request):
    try:
        async with request.app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        logger.error("Vault readiness check failed", exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "service": "apdl-llm-vault"},
        )
    return {"status": "ready", "service": "apdl-llm-vault"}


@app.get(
    "/v1/llm-connections",
    response_model=ConnectionList,
)
async def list_connections(
    request: Request,
    project_id: str = Query(pattern=PROJECT_PATTERN),
) -> ConnectionList:
    principal = require_admin(request, project_id)
    try:
        return await _store(request).list(project_id, principal.actor_user_id)
    except VaultStoreError as exc:
        raise _store_error(exc) from exc


@app.get(
    "/v1/llm-connections/{connection_id}",
    response_model=ConnectionDetail,
)
async def get_connection(
    request: Request,
    connection_id: UUID = Path(),
    project_id: str = Query(pattern=PROJECT_PATTERN),
) -> ConnectionDetail:
    principal = require_admin(request, project_id)
    try:
        return await _store(request).get(
            connection_id, project_id, principal.actor_user_id
        )
    except VaultStoreError as exc:
        raise _store_error(exc) from exc


@app.post(
    "/v1/llm-connections",
    response_model=ConnectionDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_connection(
    body: CreateConnectionRequest,
    request: Request,
) -> ConnectionDetail:
    principal = require_admin(request, body.project_id)
    try:
        model_ids = await discover_model_ids(
            body.provider, body.api_key.get_secret_value()
        )
        projections = await _project_models(
            _projector(request), tuple(body.consumers), body.provider, model_ids
        )
        return await _store(request).create(
            project_id=body.project_id,
            provider=body.provider,
            label=body.label,
            api_key=body.api_key.get_secret_value(),
            consumers=tuple(body.consumers),
            model_ids=model_ids,
            projections=projections,
            actor_user_id=principal.actor_user_id,
        )
    except ProviderDiscoveryError as exc:
        raise _discovery_error(exc) from exc
    except ProjectionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VaultStoreError as exc:
        raise _store_error(exc) from exc


@app.put(
    "/v1/llm-connections/{connection_id}",
    response_model=ConnectionDetail,
)
async def replace_connection(
    body: ReplaceConnectionRequest,
    request: Request,
    connection_id: UUID = Path(),
) -> ConnectionDetail:
    principal = require_admin(request, body.project_id)
    try:
        authority = await _store(request).load_mutation_authority(
            connection_id=connection_id,
            project_id=body.project_id,
            expected_version=body.version,
            actor_user_id=principal.actor_user_id,
        )
        if authority.provider != body.provider:
            raise VaultConflictError("Connection provider cannot be changed")
        model_ids = await discover_model_ids(
            body.provider, body.api_key.get_secret_value()
        )
        projections = await _project_models(
            _projector(request), tuple(body.consumers), body.provider, model_ids
        )
        return await _store(request).replace(
            authority=authority,
            label=body.label,
            api_key=body.api_key.get_secret_value(),
            consumers=tuple(body.consumers),
            model_ids=model_ids,
            projections=projections,
            actor_user_id=principal.actor_user_id,
        )
    except ProviderDiscoveryError as exc:
        raise _discovery_error(exc) from exc
    except ProjectionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VaultStoreError as exc:
        raise _store_error(exc) from exc


@app.post(
    "/v1/llm-connections/{connection_id}/refresh",
    response_model=ConnectionDetail,
)
async def refresh_connection(
    body: RefreshConnectionRequest,
    request: Request,
    connection_id: UUID = Path(),
) -> ConnectionDetail:
    principal = require_admin(request, body.project_id)
    try:
        authority = await _store(request).load_refresh_authority(
            connection_id=connection_id,
            project_id=body.project_id,
            expected_version=body.version,
            actor_user_id=principal.actor_user_id,
        )
        model_ids = await discover_model_ids(
            authority.provider, authority.api_key
        )
        projections = await _project_models(
            _projector(request),
            authority.consumers,
            authority.provider,
            model_ids,
        )
        return await _store(request).refresh(
            authority=authority,
            model_ids=model_ids,
            projections=projections,
            actor_user_id=principal.actor_user_id,
        )
    except ProviderDiscoveryError as exc:
        raise _discovery_error(exc) from exc
    except ProjectionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except VaultStoreError as exc:
        raise _store_error(exc) from exc


@app.post(
    "/v1/llm-connections/{connection_id}/revoke",
    response_model=ConnectionSummary,
)
async def revoke_connection(
    body: RevokeConnectionRequest,
    request: Request,
    connection_id: UUID = Path(),
) -> ConnectionSummary:
    principal = require_admin(request, body.project_id)
    try:
        authority = await _store(request).load_mutation_authority(
            connection_id=connection_id,
            project_id=body.project_id,
            expected_version=body.version,
            actor_user_id=principal.actor_user_id,
        )
        return await _store(request).revoke(
            authority=authority,
            reason=body.reason,
            actor_user_id=principal.actor_user_id,
        )
    except VaultStoreError as exc:
        raise _store_error(exc) from exc


@app.post(
    "/internal/v1/credential-access",
    response_model=CredentialAccessResponse,
)
async def issue_credential_access(
    body: CredentialAccessRequest,
    request: Request,
) -> CredentialAccessResponse:
    require_consumer(request, body.consumer)
    try:
        return await _store(request).issue_access(body)
    except VaultNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VaultStoreError as exc:
        raise _store_error(exc) from exc
