"""AES-256-GCM envelope for vault-owned provider credentials."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultKeyConfigurationError(RuntimeError):
    """The platform vault encryption key is absent or invalid."""


class VaultDecryptionError(RuntimeError):
    """Stored credential bytes failed authenticated decryption."""


@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    encryption_key_id: str


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise VaultKeyConfigurationError(
                "LLM vault encryption key must decode to exactly 32 bytes"
            )
        self._cipher = AESGCM(key)
        self.key_id = f"sha256:{hashlib.sha256(key).hexdigest()[:32]}"

    @classmethod
    def from_base64(cls, encoded: str) -> "CredentialCipher":
        if not encoded or encoded != encoded.strip() or not encoded.isascii():
            raise VaultKeyConfigurationError(
                "LLM vault encryption key is missing or malformed"
            )
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise VaultKeyConfigurationError(
                "LLM vault encryption key is missing or malformed"
            ) from exc
        if base64.b64encode(key).decode("ascii") != encoded:
            raise VaultKeyConfigurationError(
                "LLM vault encryption key is missing or malformed"
            )
        return cls(key)

    @staticmethod
    def _associated_data(
        *,
        credential_id: UUID,
        connection_id: UUID,
        project_id: str,
        provider: str,
        credential_version: int,
    ) -> bytes:
        return json.dumps(
            {
                "connection_id": str(connection_id),
                "credential_id": str(credential_id),
                "credential_version": credential_version,
                "project_id": project_id,
                "provider": provider,
                "schema_version": "llm_vault_provider_secret@1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def encrypt(
        self,
        api_key: str,
        *,
        credential_id: UUID,
        connection_id: UUID,
        project_id: str,
        provider: str,
        credential_version: int,
    ) -> EncryptedSecret:
        raw = api_key.encode("utf-8")
        if not raw or len(raw) > 16_384 or "\x00" in api_key:
            raise ValueError("provider API key must contain 1-16384 UTF-8 bytes")
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            raw,
            self._associated_data(
                credential_id=credential_id,
                connection_id=connection_id,
                project_id=project_id,
                provider=provider,
                credential_version=credential_version,
            ),
        )
        return EncryptedSecret(ciphertext, nonce, self.key_id)

    def decrypt(
        self,
        *,
        ciphertext: bytes,
        nonce: bytes,
        encryption_key_id: str,
        credential_id: UUID,
        connection_id: UUID,
        project_id: str,
        provider: str,
        credential_version: int,
    ) -> str:
        if encryption_key_id != self.key_id:
            raise VaultDecryptionError("credential uses a different vault key")
        try:
            raw = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._associated_data(
                    credential_id=credential_id,
                    connection_id=connection_id,
                    project_id=project_id,
                    provider=provider,
                    credential_version=credential_version,
                ),
            )
            return raw.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise VaultDecryptionError(
                "credential failed authenticated decryption"
            ) from exc
