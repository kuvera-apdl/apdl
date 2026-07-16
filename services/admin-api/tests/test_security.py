from __future__ import annotations

import hashlib
import json

import pytest

from app.config import Settings
from app.security import hash_password, token_hash, verify_password


def test_argon2id_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("a-correct-horse-battery-staple")
    second = hash_password("a-correct-horse-battery-staple")

    assert first.startswith("$argon2id$")
    assert first != second
    assert verify_password(first, "a-correct-horse-battery-staple")
    assert not verify_password(first, "wrong-password")


def test_session_tokens_use_a_one_way_digest() -> None:
    assert token_hash("opaque-session") == hashlib.sha256(b"opaque-session").hexdigest()
    assert "opaque-session" not in token_hash("opaque-session")


def test_settings_reject_a_service_key_for_another_project(monkeypatch) -> None:
    monkeypatch.setenv(
        "APDL_SERVICE_API_KEYS",
        json.dumps({"other": "proj_demo_0123456789abcdef"}),
    )
    monkeypatch.delenv("APDL_DEV_API_KEY", raising=False)

    with pytest.raises(ValueError, match="does not belong"):
        Settings.from_env()


def test_settings_reject_wildcard_origins(monkeypatch) -> None:
    monkeypatch.setenv("APDL_SERVICE_API_KEYS", "{}")
    monkeypatch.delenv("APDL_DEV_API_KEY", raising=False)
    monkeypatch.setenv("APDL_ADMIN_ALLOWED_ORIGINS", '["*"]')

    with pytest.raises(ValueError, match="Invalid admin origin"):
        Settings.from_env()


def test_settings_allow_both_local_console_ports_by_default(monkeypatch) -> None:
    monkeypatch.setenv("APDL_SERVICE_API_KEYS", "{}")
    monkeypatch.delenv("APDL_DEV_API_KEY", raising=False)
    monkeypatch.delenv("APDL_ADMIN_ALLOWED_ORIGINS", raising=False)

    settings = Settings.from_env()

    assert settings.allowed_origins == frozenset(
        {"http://localhost:5173", "http://localhost:5174"}
    )
    assert settings.stream_authority_check_seconds == 5.0
    assert settings.upstream_read_timeout_seconds == 60.0
    assert settings.readiness_probe_timeout_seconds == 2.0


@pytest.mark.parametrize(
    "name",
    [
        "APDL_ADMIN_STREAM_AUTH_CHECK_SECONDS",
        "APDL_ADMIN_UPSTREAM_READ_TIMEOUT_SECONDS",
        "APDL_ADMIN_READINESS_PROBE_TIMEOUT_SECONDS",
    ],
)
def test_settings_reject_invalid_admin_durations(monkeypatch, name: str) -> None:
    monkeypatch.setenv("APDL_SERVICE_API_KEYS", "{}")
    monkeypatch.delenv("APDL_DEV_API_KEY", raising=False)
    monkeypatch.setenv(name, "0")

    with pytest.raises(ValueError, match="positive duration"):
        Settings.from_env()
