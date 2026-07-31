"""Workload-authenticated, just-in-time access to vault-owned credentials."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr


Provider = Literal["anthropic", "openai", "google", "xai"]
PROVIDERS = frozenset({"anthropic", "openai", "google", "xai"})
_PROJECT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


class CredentialConfigurationError(RuntimeError):
    """The vault client configuration is unusable."""


class CredentialStoreError(RuntimeError):
    """A vault access operation failed without exposing secret material."""


class CredentialNotFoundError(CredentialStoreError):
    """The exact active vault credential authority is absent."""


class CredentialConflictError(CredentialStoreError):
    """Credential lifecycle is managed exclusively by the vault."""


class CredentialDecryptionError(CredentialStoreError):
    """The vault could not materialize the credential safely."""


@dataclass(frozen=True)
class DecryptedCredential:
    credential_id: UUID
    project_id: str
    provider: Provider
    credential_version: int
    api_key: str = field(repr=False)


class _AccessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["llm_credential_access@1"]
    access_id: UUID
    connection_id: UUID
    credential_id: UUID
    credential_version: int = Field(ge=1)
    provider: Provider
    api_key: StrictStr = Field(min_length=1, max_length=16_384, repr=False)


def canonical_provider(provider: str) -> Provider:
    if provider not in PROVIDERS:
        raise ValueError("provider must be anthropic, openai, google, or xai")
    return cast(Provider, provider)


def validate_scope(project_id: str, provider: str) -> Provider:
    if _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("project_id must match ^[A-Za-z0-9]{1,64}$")
    return canonical_provider(provider)


def _configuration() -> tuple[str, str]:
    base_url = os.getenv("LLM_VAULT_URL", "").rstrip("/")
    token = os.getenv("LLM_VAULT_CODEGEN_TOKEN", "")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CredentialConfigurationError(
            "LLM_VAULT_URL must be an absolute HTTP URL"
        )
    if token != token.strip() or len(token.encode("utf-8")) < 32:
        raise CredentialConfigurationError(
            "LLM_VAULT_CODEGEN_TOKEN must contain at least 32 normalized bytes"
        )
    return base_url, token


class ProjectCredentialStore:
    """Obtain one exact credential only at the provider egress boundary."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        token: str,
        owns_client: bool = False,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token = token
        self._owns_client = owns_client

    @classmethod
    def from_environment(
        cls, client: httpx.AsyncClient | None = None
    ) -> "ProjectCredentialStore":
        base_url, token = _configuration()
        owns_client = client is None
        return cls(
            client or httpx.AsyncClient(),
            base_url=base_url,
            token=token,
            owns_client=owns_client,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    async def lock_pair(conn: Any, project_id: str, provider: str) -> None:
        canonical = validate_scope(project_id, provider)
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"apdl:llm-vault:{project_id}:{canonical}",
        )

    async def load_active(
        self,
        project_id: str,
        provider: str,
        *,
        credential_id: UUID,
        credential_version: int,
        execution_id: str,
        purpose: str,
    ) -> DecryptedCredential:
        canonical = validate_scope(project_id, provider)
        if credential_version < 1:
            raise ValueError("credential_version must be positive")
        if (
            execution_id != execution_id.strip()
            or not 1 <= len(execution_id) <= 256
            or "\r" in execution_id
            or "\n" in execution_id
        ):
            raise ValueError("execution_id is invalid")
        if _PURPOSE.fullmatch(purpose) is None:
            raise ValueError("purpose is invalid")
        try:
            response = await self._client.post(
                f"{self._base_url}/internal/v1/credential-access",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "schema_version": "llm_credential_access_request@1",
                    "project_id": project_id,
                    "provider": canonical,
                    "consumer": "codegen",
                    "execution_id": execution_id,
                    "purpose": purpose,
                    "expected_credential_id": str(credential_id),
                    "expected_credential_version": credential_version,
                },
                timeout=5.0,
            )
            if response.status_code == 404:
                raise CredentialNotFoundError(
                    "Exact project credential authority is unavailable"
                )
            response.raise_for_status()
            value = _AccessResponse.model_validate_json(response.content)
        except CredentialNotFoundError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise CredentialStoreError(
                "Project credential vault access is unavailable"
            ) from exc
        if (
            value.credential_id != credential_id
            or value.credential_version != credential_version
            or value.provider != canonical
        ):
            raise CredentialStoreError(
                "Project credential vault returned mismatched authority"
            )
        return DecryptedCredential(
            credential_id=value.credential_id,
            project_id=project_id,
            provider=canonical,
            credential_version=value.credential_version,
            api_key=value.api_key,
        )
