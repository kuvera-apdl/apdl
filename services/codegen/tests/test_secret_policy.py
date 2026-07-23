"""Canonical secret policy shared by every Codegen text boundary."""

from __future__ import annotations

import pytest

from app.safety.secrets import (
    REDACTION_MARKER,
    SecretScanLimitError,
    contains_secret,
    redact_secrets,
    secret_environment_name,
    secret_kinds,
    structured_value_contains_secret,
)


@pytest.mark.parametrize(
    ("kind", "secret"),
    [
        ("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE"),
        ("github_token", "ghp_" + "a" * 40),
        ("github_fine_grained_token", "github_pat_" + "b" * 32),
        ("gitlab_token", "glpat-" + "c" * 24),
        ("npm_token", "npm_" + "d" * 36),
        ("slack_token", "xoxb-" + "e" * 20),
        ("provider_secret_key", "sk-proj-" + "f" * 24),
        ("google_api_key", "AIza" + "g" * 30),
        ("json_web_token", "eyJabcdefgh.ijklmnop.qrstuvwx"),
    ],
)
def test_common_tokens_are_detected_and_redacted(kind: str, secret: str) -> None:
    assert kind in secret_kinds(secret)
    redacted, changed = redact_secrets(f"prefix {secret} suffix")

    assert changed is True
    assert secret not in redacted
    assert REDACTION_MARKER in redacted


@pytest.mark.parametrize(
    ("kind", "secret"),
    [
        (
            "credential_url",
            "https://service-user:service-password@example.test/private",
        ),
        ("url_query_secret", "https://example.test/?access_token=top-secret-value"),
        ("authorization_header", "Authorization: Bearer opaque-bearer-value"),
        ("authorization_header", "Proxy-Authorization=Basic dXNlcjpwYXNz"),
        ("cookie_header", "Set-Cookie: session=opaque-session-value"),
        ("named_secret", "password=hunter2"),
        ("named_secret", "_authToken=npm-registry-secret"),
        ("named_secret", "AWS_SESSION_TOKEN=temporary-cloud-session-token-123456"),
        ("named_secret", '"client_secret": "quoted secret with spaces"'),
        (
            "private_key",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-material\n"
            "-----END OPENSSH PRIVATE KEY-----",
        ),
    ],
)
def test_contextual_credentials_are_detected_and_redacted(
    kind: str,
    secret: str,
) -> None:
    assert kind in secret_kinds(secret)
    redacted, changed = redact_secrets(secret)

    assert changed is True
    assert secret not in redacted
    assert contains_secret(redacted) is False


def test_configured_values_and_bytes_use_the_same_policy() -> None:
    configured = "provider-value-without-a-known-prefix"

    assert contains_secret(
        f"value={configured}".encode(),
        protected_values=(configured,),
    )
    redacted, changed = redact_secrets(
        f"value={configured}",
        protected_values=(configured,),
    )

    assert changed is True
    assert redacted == f"value={REDACTION_MARKER}"


def test_redaction_is_idempotent_and_clean_text_is_unchanged() -> None:
    value = "Authorization: Bearer opaque-bearer-value"
    first, first_changed = redact_secrets(value)
    second, second_changed = redact_secrets(first)

    assert first_changed is True
    assert second_changed is False
    assert second == first
    assert redact_secrets("ordinary build output") == ("ordinary build output", False)


def test_structured_output_scans_keys_values_and_protected_values() -> None:
    configured = "configured-provider-secret"

    assert structured_value_contains_secret(
        {"notes": ["ok", configured]},
        protected_values=(configured,),
    )
    assert structured_value_contains_secret(
        {"Authorization: Bearer opaque-bearer-value": "ok"}
    )
    assert structured_value_contains_secret({"notes": ["clean"]}) is False


def test_scans_fail_closed_at_explicit_work_budgets() -> None:
    with pytest.raises(SecretScanLimitError, match="byte limit"):
        contains_secret("x" * 9, max_bytes=8)
    with pytest.raises(SecretScanLimitError, match="node limit"):
        structured_value_contains_secret(["a", "b"], max_nodes=2)
    with pytest.raises(SecretScanLimitError, match="aggregate text"):
        structured_value_contains_secret(["abcd", "efgh"], max_text_bytes=7)
    with pytest.raises(SecretScanLimitError, match="protected secret values"):
        contains_secret("safe", protected_values=("x" * (256 * 1024 + 1),))


def test_environment_secret_names_are_canonical_and_strict() -> None:
    assert secret_environment_name("OPENAI_API_KEY") is True
    assert secret_environment_name("DATABASE_URL") is True
    assert secret_environment_name("PUBLIC_BUILD_MODE") is False
    with pytest.raises(TypeError):
        secret_environment_name(123)
