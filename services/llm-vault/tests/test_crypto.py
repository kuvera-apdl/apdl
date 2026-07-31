from __future__ import annotations

import base64
from uuid import UUID

import pytest

from app.crypto import (
    CredentialCipher,
    VaultDecryptionError,
    VaultKeyConfigurationError,
)


KEY = base64.b64encode(bytes(range(32))).decode("ascii")
CREDENTIAL_ID = UUID("10000000-0000-4000-8000-000000000001")
CONNECTION_ID = UUID("20000000-0000-4000-8000-000000000002")


def test_round_trip_binds_every_authority_field() -> None:
    cipher = CredentialCipher.from_base64(KEY)
    encrypted = cipher.encrypt(
        "provider-secret",
        credential_id=CREDENTIAL_ID,
        connection_id=CONNECTION_ID,
        project_id="demo",
        provider="openai",
        credential_version=3,
    )
    assert encrypted.ciphertext != b"provider-secret"
    assert len(encrypted.nonce) == 12
    assert cipher.decrypt(
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        encryption_key_id=encrypted.encryption_key_id,
        credential_id=CREDENTIAL_ID,
        connection_id=CONNECTION_ID,
        project_id="demo",
        provider="openai",
        credential_version=3,
    ) == "provider-secret"

    with pytest.raises(VaultDecryptionError):
        cipher.decrypt(
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            encryption_key_id=encrypted.encryption_key_id,
            credential_id=CREDENTIAL_ID,
            connection_id=CONNECTION_ID,
            project_id="other",
            provider="openai",
            credential_version=3,
        )


@pytest.mark.parametrize("encoded", ["", "not-base64", base64.b64encode(b"short").decode()])
def test_rejects_invalid_platform_keys(encoded: str) -> None:
    with pytest.raises(VaultKeyConfigurationError):
        CredentialCipher.from_base64(encoded)
