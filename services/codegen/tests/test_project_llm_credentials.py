"""Focused security contracts for project-scoped Codegen LLM credentials."""

from __future__ import annotations

import base64
import gc
import traceback
import weakref
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.store.llm_credentials import (
    PROVIDERS,
    CredentialCipher,
    CredentialConfigurationError,
    CredentialDecryptionError,
    DecryptedCredential,
    EncryptedCredential,
    canonical_provider,
    rotate_active_credentials,
    validate_scope,
)


KEY_BYTES = bytes(range(32))
KEY_BASE64 = base64.b64encode(KEY_BYTES).decode("ascii")
API_KEY = "project-provider-secret-that-must-never-be-rendered"
AUTHENTICATION_ERROR = "Codegen project provider credential could not be authenticated"
ROOT = Path(__file__).resolve().parents[3]


def _cipher() -> CredentialCipher:
    return CredentialCipher.from_base64(KEY_BASE64)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        f" {KEY_BASE64}",
        f"{KEY_BASE64}\n",
        KEY_BASE64.rstrip("="),
        f"{KEY_BASE64}=",
        base64.b64encode(b"x" * 31).decode("ascii"),
        base64.b64encode(b"x" * 33).decode("ascii"),
        base64.urlsafe_b64encode(b"\xff" * 32).decode("ascii"),
        "not-base64!!",
        "é",
    ],
)
def test_encryption_key_requires_canonical_base64_of_exactly_32_bytes(
    encoded: str,
) -> None:
    with pytest.raises(CredentialConfigurationError) as captured:
        CredentialCipher.from_base64(encoded)

    rendered = "".join(traceback.format_exception(captured.value))
    assert "missing or malformed" in str(captured.value) or "exactly 32 bytes" in str(
        captured.value
    )
    assert encoded not in rendered or encoded == ""


def test_canonical_key_round_trips_and_key_material_is_not_represented() -> None:
    cipher = _cipher()

    assert cipher.key_id.startswith("sha256:")
    assert len(cipher.key_id) == len("sha256:") + 32
    assert KEY_BASE64 not in repr(cipher)
    assert KEY_BYTES.hex() not in repr(cipher)


def test_ciphertext_round_trip_and_secret_redacted_dataclass_reprs() -> None:
    cipher = _cipher()
    credential_id = uuid4()
    encrypted = cipher.encrypt(
        API_KEY,
        credential_id=credential_id,
        project_id="Project42",
        provider="anthropic",
    )

    assert len(encrypted.nonce) == 12
    assert API_KEY not in repr(encrypted)
    plaintext = cipher.decrypt(
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        credential_id=credential_id,
        project_id="Project42",
        provider="anthropic",
        encryption_key_id=cipher.key_id,
    )
    assert plaintext == API_KEY

    decrypted = DecryptedCredential(
        credential_id=credential_id,
        project_id="Project42",
        provider="anthropic",
        credential_version=1,
        api_key=plaintext,
    )
    assert API_KEY not in repr(decrypted)


def test_aes_gcm_rejects_ciphertext_and_nonce_tampering_without_secret_leak() -> None:
    cipher = _cipher()
    credential_id = uuid4()
    encrypted = cipher.encrypt(
        API_KEY,
        credential_id=credential_id,
        project_id="Project42",
        provider="openai",
    )
    tampered_ciphertext = encrypted.ciphertext[:-1] + bytes(
        [encrypted.ciphertext[-1] ^ 0x01]
    )
    tampered_nonce = bytes([encrypted.nonce[0] ^ 0x01]) + encrypted.nonce[1:]

    for ciphertext, nonce in (
        (tampered_ciphertext, encrypted.nonce),
        (encrypted.ciphertext, tampered_nonce),
        (encrypted.ciphertext, encrypted.nonce[:-1]),
    ):
        with pytest.raises(CredentialDecryptionError) as captured:
            cipher.decrypt(
                ciphertext=ciphertext,
                nonce=nonce,
                credential_id=credential_id,
                project_id="Project42",
                provider="openai",
                encryption_key_id=cipher.key_id,
            )

        assert str(captured.value) == AUTHENTICATION_ERROR
        assert API_KEY not in "".join(traceback.format_exception(captured.value))


def test_aad_rejects_cross_scope_and_cross_credential_decryption() -> None:
    cipher = _cipher()
    credential_id = uuid4()
    encrypted = cipher.encrypt(
        API_KEY,
        credential_id=credential_id,
        project_id="Project42",
        provider="anthropic",
    )
    mismatched_scopes = (
        (uuid4(), "Project42", "anthropic", cipher.key_id),
        (credential_id, "AnotherProject", "anthropic", cipher.key_id),
        (credential_id, "Project42", "openai", cipher.key_id),
        (credential_id, "Project42", "anthropic", "sha256:" + "0" * 32),
    )

    for other_id, project_id, provider, key_id in mismatched_scopes:
        with pytest.raises(CredentialDecryptionError) as captured:
            cipher.decrypt(
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                credential_id=other_id,
                project_id=project_id,
                provider=provider,
                encryption_key_id=key_id,
            )

        assert str(captured.value) == AUTHENTICATION_ERROR
        assert API_KEY not in "".join(traceback.format_exception(captured.value))


def test_ciphertext_cannot_cross_encryption_keys_even_with_matching_key_id() -> None:
    cipher = _cipher()
    other = CredentialCipher(b"z" * 32)
    credential_id = uuid4()
    encrypted = cipher.encrypt(
        API_KEY,
        credential_id=credential_id,
        project_id="Project42",
        provider="xai",
    )

    with pytest.raises(CredentialDecryptionError) as captured:
        other.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            credential_id=credential_id,
            project_id="Project42",
            provider="xai",
            encryption_key_id=other.key_id,
        )

    assert str(captured.value) == AUTHENTICATION_ERROR
    assert API_KEY not in "".join(traceback.format_exception(captured.value))


def test_provider_and_project_scope_are_strict_and_canonical() -> None:
    assert PROVIDERS == frozenset({"anthropic", "openai", "google", "xai"})
    for provider in sorted(PROVIDERS):
        assert canonical_provider(provider) == provider
        assert validate_scope("A1" * 32, provider) == provider

    for provider in (
        "Anthropic",
        "gemini",
        "azure",
        "openrouter",
        "anthropic ",
        "",
    ):
        with pytest.raises(ValueError, match="provider must be"):
            canonical_provider(provider)

    for project_id in (
        "",
        "project-with-hyphen",
        "project_with_underscore",
        " leading",
        "trailing ",
        "é",
        "A" * 65,
    ):
        with pytest.raises(ValueError, match=r"project_id must match"):
            validate_scope(project_id, "anthropic")


def test_api_key_limit_is_measured_in_utf8_bytes_and_errors_are_redacted() -> None:
    cipher = _cipher()
    credential_id = uuid4()
    exact_limit = "é" * 8_192
    encrypted = cipher.encrypt(
        exact_limit,
        credential_id=credential_id,
        project_id="Project42",
        provider="google",
    )
    assert (
        cipher.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            credential_id=credential_id,
            project_id="Project42",
            provider="google",
            encryption_key_id=cipher.key_id,
        )
        == exact_limit
    )

    oversized = f"{exact_limit}é"
    for invalid in ("", oversized):
        with pytest.raises(ValueError) as captured:
            cipher.encrypt(
                invalid,
                credential_id=uuid4(),
                project_id="Project42",
                provider="google",
            )
        assert invalid not in str(captured.value) or invalid == ""


@pytest.mark.asyncio
async def test_rotation_preflights_all_rows_and_retains_one_plaintext_at_a_time() -> None:
    """Rotation authenticates first, then streams each secret through re-encryption."""

    class TrackedPlaintext(str):
        __slots__ = ("__weakref__",)

    first_id = UUID("10000000-0000-4000-8000-000000000001")
    second_id = UUID("20000000-0000-4000-8000-000000000002")
    rows = [
        {
            "credential_id": first_id,
            "project_id": "Project1",
            "provider": "openai",
            "ciphertext": b"old-one",
            "nonce": b"1" * 12,
            "encryption_key_id": "old-key",
        },
        {
            "credential_id": second_id,
            "project_id": "Project2",
            "provider": "anthropic",
            "ciphertext": b"old-two",
            "nonce": b"2" * 12,
            "encryption_key_id": "old-key",
        },
    ]
    plaintext_refs: list[weakref.ReferenceType[TrackedPlaintext]] = []
    events: list[str] = []
    stored = {
        first_id: {
            "ciphertext": b"old-one",
            "nonce": b"1" * 12,
            "encryption_key_id": "old-key",
        },
        second_id: {
            "ciphertext": b"old-two",
            "nonce": b"2" * 12,
            "encryption_key_id": "old-key",
        },
    }

    def assert_no_plaintext_is_retained() -> None:
        gc.collect()
        assert all(reference() is None for reference in plaintext_refs)

    class OldCipher:
        key_id = "old-key"

        def decrypt(self, **kwargs: object) -> str:
            assert_no_plaintext_is_retained()
            credential_id = UUID(str(kwargs["credential_id"]))
            marker = "one" if credential_id == first_id else "two"
            events.append(f"old-decrypt-{marker}")
            plaintext = TrackedPlaintext(f"secret-{marker}")
            plaintext_refs.append(weakref.ref(plaintext))
            return plaintext

    class NewCipher:
        key_id = "new-key"

        def encrypt(
            self,
            api_key: str,
            *,
            credential_id: UUID,
            project_id: str,
            provider: str,
        ) -> EncryptedCredential:
            del project_id, provider
            marker = "one" if credential_id == first_id else "two"
            assert api_key == f"secret-{marker}"
            events.append(f"new-encrypt-{marker}")
            return EncryptedCredential(
                ciphertext=f"new-{marker}".encode(),
                nonce=marker.encode().ljust(12, b"-"),
            )

        def decrypt(self, **kwargs: object) -> str:
            assert_no_plaintext_is_retained()
            credential_id = UUID(str(kwargs["credential_id"]))
            marker = "one" if credential_id == first_id else "two"
            assert kwargs["ciphertext"] == f"new-{marker}".encode()
            events.append(f"new-decrypt-{marker}")
            plaintext = TrackedPlaintext(f"secret-{marker}")
            plaintext_refs.append(weakref.ref(plaintext))
            return plaintext

    class Connection:
        def is_in_transaction(self) -> bool:
            return True

        async def fetchval(self, _query: str) -> bool:
            return True

        async def fetch(self, _query: str) -> list[dict[str, object]]:
            return rows

        async def execute(self, query: str, *args: object) -> None:
            if "UPDATE codegen_project_provider_credentials" in query:
                credential_id = UUID(str(args[0]))
                marker = "one" if credential_id == first_id else "two"
                stored[credential_id] = {
                    "ciphertext": args[1],
                    "nonce": args[2],
                    "encryption_key_id": args[3],
                }
                events.append(f"update-{marker}")
            else:
                credential_id = UUID(str(args[3]))
                marker = "one" if credential_id == first_id else "two"
                events.append(f"audit-{marker}")

        async def fetchrow(
            self,
            _query: str,
            credential_id: UUID,
        ) -> dict[str, object]:
            events.append(
                f"read-{'one' if credential_id == first_id else 'two'}"
            )
            return stored[credential_id]

    count, audit_ids = await rotate_active_credentials(
        Connection(),
        old_cipher=OldCipher(),  # type: ignore[arg-type]
        new_cipher=NewCipher(),  # type: ignore[arg-type]
        actor="operator:test",
    )

    assert count == 2
    assert len(audit_ids) == 2
    assert events == [
        "old-decrypt-one",
        "old-decrypt-two",
        "old-decrypt-one",
        "new-encrypt-one",
        "update-one",
        "audit-one",
        "old-decrypt-two",
        "new-encrypt-two",
        "update-two",
        "audit-two",
        "read-one",
        "new-decrypt-one",
        "read-two",
        "new-decrypt-two",
    ]
    assert_no_plaintext_is_retained()


def test_retirement_metadata_accepts_only_server_canonical_reasons() -> None:
    migration = (
        ROOT
        / "pipeline/postgres/migrations"
        / "052_codegen_project_provider_credentials.sql"
    ).read_text(encoding="utf-8")

    reason_constraint = migration.split(
        "codegen_project_provider_credentials_reason_check", 1
    )[1].split(
        "codegen_project_provider_credentials_lifecycle_check", 1
    )[0]
    assert "'provider_connection_replaced'" in reason_constraint
    assert "'provider_connection_revoked'" in reason_constraint
    assert "LENGTH(retirement_reason)" not in reason_constraint
    assert "BTRIM(retirement_reason)" not in reason_constraint
