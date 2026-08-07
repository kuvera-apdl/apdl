"""Application-owned resources used by governed LLM requests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import partial

from app.llm.client_cache import ProviderClientCache, build_provider_client
from app.store.llm_credentials import ProjectCredentialStore


_PROVIDER_CLIENT_CACHE_MAX_ENTRIES = 64
_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "120"))


@dataclass(frozen=True)
class LlmRuntime:
    """Long-lived credential and provider-client resources for one replica."""

    credential_store: ProjectCredentialStore
    provider_clients: ProviderClientCache

    @classmethod
    def from_environment(cls) -> "LlmRuntime":
        """Build the replica-owned runtime from validated environment settings."""
        return cls(
            credential_store=ProjectCredentialStore.from_environment(),
            provider_clients=ProviderClientCache(
                max_entries=_PROVIDER_CLIENT_CACHE_MAX_ENTRIES,
                factory=partial(
                    build_provider_client,
                    request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS,
                ),
            ),
        )

    async def aclose(self) -> None:
        """Drain provider clients before closing the credential-vault client."""
        try:
            await self.provider_clients.aclose()
        finally:
            await self.credential_store.aclose()
