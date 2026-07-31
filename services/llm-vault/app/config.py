"""Strict environment configuration for the project LLM vault."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _secret(name: str) -> str:
    value = os.getenv(name, "")
    if value != value.strip() or len(value.encode("utf-8")) < 32:
        raise ValueError(f"{name} must contain at least 32 normalized bytes")
    return value


def _url(name: str, default: str) -> str:
    value = os.getenv(name, default)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTP URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    postgres_url: str = field(repr=False)
    encryption_key_base64: str = field(repr=False)
    admin_token: str = field(repr=False)
    agents_token: str = field(repr=False)
    codegen_token: str = field(repr=False)
    projection_token: str = field(repr=False)
    agents_service_url: str
    codegen_service_url: str

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            postgres_url=os.getenv(
                "POSTGRES_URL",
                "postgresql://apdl_llm_vault:apdl_llm_vault_dev@localhost:5432/apdl",
            ),
            encryption_key_base64=os.getenv(
                "LLM_VAULT_ENCRYPTION_KEY_BASE64", ""
            ),
            admin_token=_secret("LLM_VAULT_ADMIN_TOKEN"),
            agents_token=_secret("LLM_VAULT_AGENTS_TOKEN"),
            codegen_token=_secret("LLM_VAULT_CODEGEN_TOKEN"),
            projection_token=_secret("LLM_VAULT_PROJECTION_TOKEN"),
            agents_service_url=_url(
                "AGENTS_SERVICE_URL", "http://localhost:8083"
            ),
            codegen_service_url=_url(
                "CODEGEN_SERVICE_URL", "http://localhost:8084"
            ),
        )
