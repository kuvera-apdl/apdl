"""Secret-sentinel and crypto contracts for project LLM credentials."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.store.llm_credentials import (
    CredentialCipher,
    CredentialConfigurationError,
    CredentialConflictError,
    CredentialDecryptionError,
    ProjectCredentialStore,
)


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT
    / "pipeline"
    / "postgres"
    / "migrations"
    / "048_llm_project_provider_credentials.sql"
).read_text()


def _encoded_key(value: bytes = bytes(range(32))) -> str:
    return base64.b64encode(value).decode("ascii")


@pytest.mark.parametrize(
    "value",
    ["", "not base64!", _encoded_key(b"short"), f" {_encoded_key()}"],
)
def test_encryption_key_validation_fails_closed(value: str):
    with pytest.raises(
        CredentialConfigurationError,
        match="encryption key",
    ):
        CredentialCipher.from_base64(value)


def test_ciphertext_is_bound_to_project_provider_credential_and_key():
    cipher = CredentialCipher.from_base64(_encoded_key())
    credential_id = uuid4()
    secret = "provider-secret-sentinel"

    encrypted = cipher.encrypt(
        secret,
        credential_id=credential_id,
        project_id="demo",
        provider="openai",
    )

    assert secret.encode() not in encrypted.ciphertext
    assert secret not in repr(encrypted)
    assert cipher.decrypt(
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        credential_id=credential_id,
        project_id="demo",
        provider="openai",
        encryption_key_id=cipher.key_id,
    ) == secret

    for project_id, provider, stored_id in (
        ("other", "openai", credential_id),
        ("demo", "anthropic", credential_id),
        ("demo", "openai", uuid4()),
    ):
        with pytest.raises(
            CredentialDecryptionError,
            match="could not be authenticated",
        ):
            cipher.decrypt(
                ciphertext=encrypted.ciphertext,
                nonce=encrypted.nonce,
                credential_id=stored_id,
                project_id=project_id,
                provider=provider,
                encryption_key_id=cipher.key_id,
            )


def test_tamper_and_wrong_encryption_key_fail_without_secret_material():
    credential_id = uuid4()
    secret = "never-include-this-provider-secret"
    cipher = CredentialCipher.from_base64(_encoded_key())
    other_cipher = CredentialCipher.from_base64(_encoded_key(bytes(range(1, 33))))
    encrypted = cipher.encrypt(
        secret,
        credential_id=credential_id,
        project_id="demo",
        provider="xai",
    )
    tampered = encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1])

    with pytest.raises(CredentialDecryptionError) as tamper_error:
        cipher.decrypt(
            ciphertext=tampered,
            nonce=encrypted.nonce,
            credential_id=credential_id,
            project_id="demo",
            provider="xai",
            encryption_key_id=cipher.key_id,
        )
    with pytest.raises(CredentialDecryptionError) as key_error:
        other_cipher.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            credential_id=credential_id,
            project_id="demo",
            provider="xai",
            encryption_key_id=cipher.key_id,
        )

    assert secret not in str(tamper_error.value)
    assert secret not in str(key_error.value)


def test_migration_enforces_one_active_crypto_shreddable_credential():
    assert "CREATE TABLE llm_project_provider_credentials" in MIGRATION
    assert "provider IN ('openai', 'anthropic', 'google', 'xai')" in MIGRATION
    assert "WHERE state = 'active'" in MIGRATION
    assert "OCTET_LENGTH(nonce) = 12" in MIGRATION
    assert "state = 'active'" in MIGRATION
    assert "state = 'replaced'" in MIGRATION
    assert "state = 'revoked'" in MIGRATION
    assert "ciphertext IS NULL" in MIGRATION
    assert "nonce IS NULL" in MIGRATION
    assert "DEFERRABLE INITIALLY DEFERRED" in MIGRATION
    assert "llm_project_provider_credential_audit_no_update_delete" in MIGRATION
    assert "llm_project_provider_credential_audit_no_truncate" in MIGRATION
    assert "plaintext" not in MIGRATION.lower()


class _CreateConnection:
    def __init__(self, *, active_exists: bool) -> None:
        self.active_exists = active_exists
        self.insert_args: tuple[object, ...] | None = None

    async def execute(self, query: str, *args):
        return "OK"

    async def fetchrow(self, query: str, *args):
        if "AS next_version" in query:
            return {
                "next_version": 3,
                "active_exists": self.active_exists,
            }
        if "INSERT INTO llm_project_provider_credentials" in query:
            self.insert_args = args
            return {
                "credential_id": args[0],
                "project_id": args[1],
                "provider": args[2],
                "credential_version": args[3],
                "state": "active",
                "encryption_key_id": args[6],
                "created_by_actor": args[7],
                "created_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
                "retired_by_actor": None,
                "retirement_reason": None,
                "retired_at": None,
            }
        raise AssertionError(query)


@pytest.mark.asyncio
async def test_create_after_revocation_advances_historical_version() -> None:
    conn = _CreateConnection(active_exists=False)
    store = ProjectCredentialStore(
        pool=None,
        cipher=CredentialCipher.from_base64(_encoded_key()),
    )

    metadata = await store.create_in_transaction(
        conn,
        "demo",
        "openai",
        "new-secret",
        actor="operator:test",
    )

    assert metadata.credential_version == 3
    assert conn.insert_args is not None
    assert conn.insert_args[3] == 3


@pytest.mark.asyncio
async def test_create_rejects_existing_active_version() -> None:
    conn = _CreateConnection(active_exists=True)
    store = ProjectCredentialStore(
        pool=None,
        cipher=CredentialCipher.from_base64(_encoded_key()),
    )

    with pytest.raises(
        CredentialConflictError,
        match="already exists",
    ):
        await store.create_in_transaction(
            conn,
            "demo",
            "openai",
            "new-secret",
            actor="operator:test",
        )

    assert conn.insert_args is None
