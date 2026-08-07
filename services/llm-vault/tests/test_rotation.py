"""Plaintext-lifetime and atomicity contracts for vault key rotation."""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

import pytest

from app.crypto import CredentialCipher, EncryptedSecret, VaultDecryptionError
from app.rotation import rotate_active_credentials


OLD_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
NEW_KEY = base64.b64encode(bytes(reversed(range(32)))).decode("ascii")


class _RecordingCipher:
    def __init__(
        self,
        cipher: CredentialCipher,
        *,
        label: str,
        timeline: list[tuple[str, UUID]],
        fail_decrypt_on: int | None = None,
    ) -> None:
        self._cipher = cipher
        self.key_id = cipher.key_id
        self._label = label
        self._timeline = timeline
        self._fail_decrypt_on = fail_decrypt_on
        self.decrypt_count = 0
        self.encrypt_count = 0

    def decrypt(self, **kwargs: Any) -> str:
        credential_id = kwargs["credential_id"]
        assert isinstance(credential_id, UUID)
        self.decrypt_count += 1
        self._timeline.append((f"{self._label}:decrypt", credential_id))
        if self.decrypt_count == self._fail_decrypt_on:
            raise VaultDecryptionError("injected rotation decryption failure")
        return self._cipher.decrypt(**kwargs)

    def encrypt(
        self,
        plaintext: str,
        **kwargs: Any,
    ) -> EncryptedSecret:
        credential_id = kwargs["credential_id"]
        assert isinstance(credential_id, UUID)
        self.encrypt_count += 1
        self._timeline.append((f"{self._label}:encrypt", credential_id))
        return self._cipher.encrypt(plaintext, **kwargs)


class _RecordingConnection:
    def __init__(
        self,
        rows: tuple[dict[str, object], ...],
        timeline: list[tuple[str, UUID]],
    ) -> None:
        self._rows = {
            UUID(str(row["credential_id"])): dict(row) for row in rows
        }
        self._timeline = timeline

    def is_in_transaction(self) -> bool:
        return True

    async def fetchval(self, _query: str) -> bool:
        return True

    async def fetch(self, _query: str) -> tuple[dict[str, object], ...]:
        return tuple(dict(row) for row in self._rows.values())

    async def execute(self, query: str, *args: object) -> str:
        if "UPDATE llm_vault_provider_secrets" in query:
            credential_id = args[0]
            assert isinstance(credential_id, UUID)
            self._timeline.append(("database:update", credential_id))
            self._rows[credential_id].update(
                ciphertext=args[1],
                nonce=args[2],
                encryption_key_id=args[3],
            )
            return "UPDATE 1"
        if "INSERT INTO llm_vault_key_rotation_audit" in query:
            credential_id = args[4]
            assert isinstance(credential_id, UUID)
            self._timeline.append(("database:audit", credential_id))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected rotation statement: {query}")

    async def fetchrow(
        self,
        _query: str,
        credential_id: UUID,
    ) -> dict[str, object] | None:
        row = self._rows.get(credential_id)
        if row is None:
            return None
        return {
            "ciphertext": row["ciphertext"],
            "nonce": row["nonce"],
            "encryption_key_id": row["encryption_key_id"],
        }


def _encrypted_rows(
    cipher: CredentialCipher,
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for index in (1, 2):
        credential_id = UUID(int=index)
        connection_id = UUID(int=100 + index)
        project_id = f"project-{index}"
        credential_version = index
        encrypted = cipher.encrypt(
            f"provider-secret-{index}",
            credential_id=credential_id,
            connection_id=connection_id,
            project_id=project_id,
            provider="openai",
            credential_version=credential_version,
        )
        rows.append(
            {
                "credential_id": credential_id,
                "connection_id": connection_id,
                "project_id": project_id,
                "provider": "openai",
                "credential_version": credential_version,
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "encryption_key_id": encrypted.encryption_key_id,
            }
        )
    return tuple(rows)


@pytest.mark.asyncio
async def test_rotation_decrypts_each_credential_twice_without_plaintext_plan() -> None:
    old_delegate = CredentialCipher.from_base64(OLD_KEY)
    new_delegate = CredentialCipher.from_base64(NEW_KEY)
    rows = _encrypted_rows(old_delegate)
    credential_ids = tuple(UUID(str(row["credential_id"])) for row in rows)
    timeline: list[tuple[str, UUID]] = []
    old_cipher = _RecordingCipher(
        old_delegate,
        label="old",
        timeline=timeline,
    )
    new_cipher = _RecordingCipher(
        new_delegate,
        label="new",
        timeline=timeline,
    )
    conn = _RecordingConnection(rows, timeline)

    count, audit_ids = await rotate_active_credentials(
        conn,
        old_cipher=old_cipher,
        new_cipher=new_cipher,
        operator="test:rotation",
    )

    assert count == 2
    assert len(audit_ids) == 2
    assert old_cipher.decrypt_count == 2
    assert new_cipher.encrypt_count == 2
    assert new_cipher.decrypt_count == 2
    assert timeline == [
        ("old:decrypt", credential_ids[0]),
        ("new:encrypt", credential_ids[0]),
        ("old:decrypt", credential_ids[1]),
        ("new:encrypt", credential_ids[1]),
        ("database:update", credential_ids[0]),
        ("database:audit", credential_ids[0]),
        ("database:update", credential_ids[1]),
        ("database:audit", credential_ids[1]),
        ("new:decrypt", credential_ids[0]),
        ("new:decrypt", credential_ids[1]),
    ]


@pytest.mark.asyncio
async def test_rotation_authenticates_complete_set_before_any_write() -> None:
    old_delegate = CredentialCipher.from_base64(OLD_KEY)
    rows = _encrypted_rows(old_delegate)
    timeline: list[tuple[str, UUID]] = []
    old_cipher = _RecordingCipher(
        old_delegate,
        label="old",
        timeline=timeline,
        fail_decrypt_on=2,
    )
    new_cipher = _RecordingCipher(
        CredentialCipher.from_base64(NEW_KEY),
        label="new",
        timeline=timeline,
    )
    conn = _RecordingConnection(rows, timeline)

    with pytest.raises(
        VaultDecryptionError,
        match="injected rotation decryption failure",
    ):
        await rotate_active_credentials(
            conn,
            old_cipher=old_cipher,
            new_cipher=new_cipher,
            operator="test:rotation",
        )

    assert all(not action.startswith("database:") for action, _ in timeline)


@pytest.mark.asyncio
async def test_rotation_propagates_new_key_verification_failure() -> None:
    old_delegate = CredentialCipher.from_base64(OLD_KEY)
    rows = _encrypted_rows(old_delegate)
    timeline: list[tuple[str, UUID]] = []
    old_cipher = _RecordingCipher(
        old_delegate,
        label="old",
        timeline=timeline,
    )
    new_cipher = _RecordingCipher(
        CredentialCipher.from_base64(NEW_KEY),
        label="new",
        timeline=timeline,
        fail_decrypt_on=1,
    )
    conn = _RecordingConnection(rows, timeline)

    with pytest.raises(
        VaultDecryptionError,
        match="injected rotation decryption failure",
    ):
        await rotate_active_credentials(
            conn,
            old_cipher=old_cipher,
            new_cipher=new_cipher,
            operator="test:rotation",
        )

    assert [action for action, _ in timeline].count("database:update") == 2
    assert [action for action, _ in timeline].count("database:audit") == 2
