"""Environment-derived configuration for the codegen service.

Mirrors the ``os.getenv`` convention used across APDL services (no settings
framework). Values are read at call time so tests can monkeypatch the
environment without re-importing the module.
"""

from __future__ import annotations

import os


def postgres_url() -> str:
    """DSN for the shared APDL PostgreSQL database."""
    return os.getenv("POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl")


def internal_token() -> str:
    """Shared internal service token (``X-APDL-Internal-Token``).

    Empty in local dev, in which case the internal-token guard is permissive —
    matching the posture of the other services.
    """
    return os.getenv("APDL_INTERNAL_TOKEN", "")


def github_app_id() -> str:
    """The GitHub App's numeric ID (as a string)."""
    return os.getenv("GITHUB_APP_ID", "")


def github_app_private_key() -> str:
    """The GitHub App's PEM private key, provided inline or via a file path."""
    inline = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if inline:
        return inline
    path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", "")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    return ""


def github_api_url() -> str:
    """Base URL for the GitHub REST API (override for GitHub Enterprise)."""
    return os.getenv("GITHUB_API_URL", "https://api.github.com")


def github_webhook_secret() -> str:
    """HMAC secret for verifying inbound GitHub webhooks. Empty = permissive dev."""
    return os.getenv("GITHUB_WEBHOOK_SECRET", "")
