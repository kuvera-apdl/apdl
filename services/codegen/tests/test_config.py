"""Tests for env-derived config — focused on GitHub App private-key resolution.

The key must load cleanly from a single-line ``.env`` value (the Docker case) as
well as from a file, so these cover inline (incl. escaped newlines), base64, the
``~``-expanded path, precedence, and the empty fallback.
"""

import base64

from app import config

_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBVQIBADAN\n-----END RSA PRIVATE KEY-----\n"

_KEY_VARS = (
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_BASE64",
    "GITHUB_APP_PRIVATE_KEY_PATH",
)


def _clear(monkeypatch):
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_inline_key_returned_trimmed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PEM)
    assert config.github_app_private_key() == _PEM.strip()


def test_inline_key_unescapes_single_line_newlines(monkeypatch):
    _clear(monkeypatch)
    # The shape a PEM takes when squeezed onto one .env line.
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PEM.replace("\n", "\\n"))
    resolved = config.github_app_private_key()
    assert "\n" in resolved and "\\n" not in resolved
    assert resolved == _PEM.strip()


def test_base64_key_is_decoded(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(_PEM.encode()).decode()
    )
    assert config.github_app_private_key() == _PEM


def test_path_key_expands_tilde(monkeypatch, tmp_path):
    _clear(monkeypatch)
    (tmp_path / "key.pem").write_text(_PEM)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "~/key.pem")
    assert config.github_app_private_key() == _PEM


def test_inline_beats_base64_and_path(monkeypatch, tmp_path):
    (tmp_path / "k.pem").write_text("FROM_PATH")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "FROM_INLINE")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(b"FROM_B64").decode())
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "k.pem"))
    assert config.github_app_private_key() == "FROM_INLINE"


def test_base64_beats_path(monkeypatch, tmp_path):
    _clear(monkeypatch)
    (tmp_path / "k.pem").write_text("FROM_PATH")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(b"FROM_B64").decode())
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "k.pem"))
    assert config.github_app_private_key() == "FROM_B64"


def test_empty_when_nothing_set(monkeypatch):
    _clear(monkeypatch)
    assert config.github_app_private_key() == ""


def test_invalid_base64_falls_back_to_empty(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", "not%%%base64%%%")
    assert config.github_app_private_key() == ""


def test_cors_origins_default_to_local_admin(monkeypatch):
    monkeypatch.delenv("CODEGEN_CORS_ORIGINS", raising=False)
    origins = config.codegen_cors_origins()
    assert "http://localhost:5174" in origins
    assert "*" not in origins  # never wildcard — this service merges PRs


def test_cors_origins_parsed_from_env(monkeypatch):
    monkeypatch.setenv("CODEGEN_CORS_ORIGINS", "https://admin.example.com, https://ops.example.com ")
    assert config.codegen_cors_origins() == [
        "https://admin.example.com",
        "https://ops.example.com",
    ]


def test_ci_sync_max_age_defaults_to_seven_days(monkeypatch):
    monkeypatch.delenv("CODEGEN_CI_SYNC_MAX_AGE_SECONDS", raising=False)
    assert config.codegen_ci_sync_max_age_seconds() == 7 * 24 * 3600


def test_ci_sync_max_age_env_override_and_floor(monkeypatch):
    monkeypatch.setenv("CODEGEN_CI_SYNC_MAX_AGE_SECONDS", "3600")
    assert config.codegen_ci_sync_max_age_seconds() == 3600
    monkeypatch.setenv("CODEGEN_CI_SYNC_MAX_AGE_SECONDS", "-5")
    assert config.codegen_ci_sync_max_age_seconds() == 0


def test_job_budget_derives_from_the_inner_timeouts(monkeypatch):
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    monkeypatch.setenv("CODEGEN_TIMEOUT", "1800")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "300")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    # (1 + retries) × agent + clone/push slack
    assert config.codegen_job_budget() == 2 * 1800 + 2 * 300


def test_job_budget_env_override_wins(monkeypatch):
    monkeypatch.setenv("CODEGEN_JOB_BUDGET", "7200")
    assert config.codegen_job_budget() == 7200


def test_stale_sweep_interval_default_and_disable(monkeypatch):
    monkeypatch.delenv("CODEGEN_STALE_SWEEP_INTERVAL", raising=False)
    assert config.codegen_stale_sweep_interval() == 300
    monkeypatch.setenv("CODEGEN_STALE_SWEEP_INTERVAL", "0")
    assert config.codegen_stale_sweep_interval() == 0
