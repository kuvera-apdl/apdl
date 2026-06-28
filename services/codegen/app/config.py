"""Environment-derived configuration for the codegen service.

Mirrors the ``os.getenv`` convention used across APDL services (no settings
framework). Values are read at call time so tests can monkeypatch the
environment without re-importing the module.
"""

from __future__ import annotations

import base64
import os
import tempfile

_DEFAULT_MODEL = "claude-opus-4-8"


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
    r"""The GitHub App's PEM private key.

    Resolved so it works cleanly from a single-line ``.env`` (Docker) or a file
    (host), checked in this order:

    1. ``GITHUB_APP_PRIVATE_KEY`` — inline PEM. A one-line value whose newlines
       are backslash-escaped (``\n``) is restored to real newlines, so the key
       survives a ``.env`` file / compose interpolation.
    2. ``GITHUB_APP_PRIVATE_KEY_BASE64`` — base64 of the ``.pem``; the simplest
       single-line form to carry through ``.env`` (``base64 -w0 key.pem``).
    3. ``GITHUB_APP_PRIVATE_KEY_PATH`` — path to the ``.pem`` (``~`` expanded).
    """
    inline = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if inline.strip():
        # A one-line .env value often carries escaped newlines; restore them.
        if "\\n" in inline and "\n" not in inline:
            inline = inline.replace("\\n", "\n")
        return inline.strip()

    encoded = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""

    path = os.path.expanduser(os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", ""))
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


# --- Codegen editor configuration -----------------------------------------
# The in-process editor's knobs, read through getters (house style) rather than
# scattered ``os.getenv`` calls in ``AiderEditor.__init__``.


def codegen_model() -> str:
    """LiteLLM model id the editor drives (any provider key present in env)."""
    return os.getenv("CODEGEN_MODEL", _DEFAULT_MODEL)


def codegen_aider_bin() -> str:
    """Path/name of the aider executable."""
    return os.getenv("CODEGEN_AIDER_BIN", "aider")


def codegen_workdir() -> str:
    """Base directory for throwaway changeset workdirs (defaults to the tempdir)."""
    return os.getenv("CODEGEN_WORKDIR") or tempfile.gettempdir()


def codegen_keep_workdir() -> bool:
    """Keep the workdir after a run (for debugging) instead of deleting it."""
    return os.getenv("CODEGEN_KEEP_WORKDIR") == "true"


def codegen_git_timeout() -> int:
    """Per-``git``-invocation timeout, seconds."""
    return int(os.getenv("CODEGEN_GIT_TIMEOUT", "300"))


def codegen_agent_timeout() -> int:
    """Editing-agent (aider) timeout, seconds — also the per-job pipeline budget."""
    return int(os.getenv("CODEGEN_TIMEOUT", "1800"))


def codegen_test_timeout() -> int:
    """Repo test-command timeout, seconds."""
    return int(os.getenv("CODEGEN_TEST_TIMEOUT", "600"))


def codegen_max_concurrent_jobs() -> int:
    """Max changeset jobs allowed to run at once (default 1 — serialize).

    Each job runs a coding agent plus the repo's build/test, which is CPU- and
    memory-heavy; running several at once thrashes a small host. Jobs over the
    limit wait in ``queued`` until a slot frees. Floor of 1.
    """
    return max(1, int(os.getenv("CODEGEN_MAX_CONCURRENT_JOBS", "1")))
