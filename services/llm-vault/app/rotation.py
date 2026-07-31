"""Offline, all-or-nothing rotation of the vault encryption key."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.crypto import CredentialCipher


MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


class VaultRotationError(RuntimeError):
    """The vault key cannot be rotated under the current authority."""


def _operator(value: str) -> str:
    if (
        value != value.strip()
        or not 1 <= len(value) <= 512
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("operator must be a normalized single-line value")
    return value


async def rotate_active_credentials(
    conn: Any,
    *,
    old_cipher: CredentialCipher,
    new_cipher: CredentialCipher,
    operator: str,
) -> tuple[int, tuple[UUID, ...]]:
    """Re-encrypt every active credential inside one fenced transaction."""
    canonical_operator = _operator(operator)
    if old_cipher.key_id == new_cipher.key_id:
        raise ValueError("The new encryption key must differ from the old key")
    is_in_transaction = getattr(conn, "is_in_transaction", None)
    if not callable(is_in_transaction) or is_in_transaction() is not True:
        raise VaultRotationError(
            "An active PostgreSQL transaction is required for key rotation"
        )
    barrier_held = await conn.fetchval(
        """
        SELECT count(*) = 2
        FROM pg_catalog.pg_locks
        WHERE pid = pg_backend_pid()
          AND locktype = 'advisory'
          AND mode = 'ExclusiveLock'
          AND granted
          AND classid = 0
          AND objsubid = 1
          AND objid IN (
              (4158044083::BIGINT)::OID,
              (4158044084::BIGINT)::OID
          )
        """
    )
    if barrier_held is not True:
        raise VaultRotationError(
            "The exclusive APDL maintenance barrier is required for key rotation"
        )
    rows = await conn.fetch(
        """
        SELECT credential.credential_id, credential.connection_id,
               credential.project_id, credential.provider,
               credential.credential_version, secret.ciphertext,
               secret.nonce, secret.encryption_key_id
        FROM llm_vault_provider_credentials AS credential
        JOIN llm_vault_provider_secrets AS secret
          ON secret.credential_id = credential.credential_id
        WHERE credential.state = 'active'
        ORDER BY credential.project_id, credential.provider,
                 credential.connection_id
        FOR UPDATE OF credential, secret
        """
    )

    # Authenticate the complete active set before the first write. This keeps
    # a wrong or partially stale old key all-or-nothing without retaining a
    # project-wide collection of plaintext secrets in memory.
    for row in rows:
        plaintext = old_cipher.decrypt(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            encryption_key_id=str(row["encryption_key_id"]),
            credential_id=UUID(str(row["credential_id"])),
            connection_id=UUID(str(row["connection_id"])),
            project_id=str(row["project_id"]),
            provider=str(row["provider"]),
            credential_version=int(row["credential_version"]),
        )
        del plaintext

    audit_ids: list[UUID] = []
    for row in rows:
        credential_id = UUID(str(row["credential_id"]))
        connection_id = UUID(str(row["connection_id"]))
        project_id = str(row["project_id"])
        provider = str(row["provider"])
        credential_version = int(row["credential_version"])
        previous_key_id = str(row["encryption_key_id"])
        plaintext = old_cipher.decrypt(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            encryption_key_id=previous_key_id,
            credential_id=credential_id,
            connection_id=connection_id,
            project_id=project_id,
            provider=provider,
            credential_version=credential_version,
        )
        try:
            encrypted = new_cipher.encrypt(
                plaintext,
                credential_id=credential_id,
                connection_id=connection_id,
                project_id=project_id,
                provider=provider,
                credential_version=credential_version,
            )
        finally:
            del plaintext
        result = await conn.execute(
            """
            UPDATE llm_vault_provider_secrets
            SET ciphertext = $2, nonce = $3, encryption_key_id = $4
            WHERE credential_id = $1
            """,
            credential_id,
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.encryption_key_id,
        )
        if result != "UPDATE 1":
            raise VaultRotationError(
                "An active vault secret disappeared during key rotation"
            )
        audit_id = uuid4()
        await conn.execute(
            """
            INSERT INTO llm_vault_key_rotation_audit (
                audit_id, connection_id, project_id, provider,
                credential_id, credential_version, action, outcome,
                operator, previous_encryption_key_id, encryption_key_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, 'reencrypt', 'succeeded',
                $7, $8, $9
            )
            """,
            audit_id,
            connection_id,
            project_id,
            provider,
            credential_id,
            credential_version,
            canonical_operator,
            previous_key_id,
            encrypted.encryption_key_id,
        )
        audit_ids.append(audit_id)

    for row in rows:
        credential_id = UUID(str(row["credential_id"]))
        stored = await conn.fetchrow(
            """
            SELECT ciphertext, nonce, encryption_key_id
            FROM llm_vault_provider_secrets
            WHERE credential_id = $1
            """,
            credential_id,
        )
        if stored is None:
            raise VaultRotationError(
                "An active vault secret disappeared during key rotation"
            )
        plaintext = new_cipher.decrypt(
            ciphertext=bytes(stored["ciphertext"]),
            nonce=bytes(stored["nonce"]),
            encryption_key_id=str(stored["encryption_key_id"]),
            credential_id=credential_id,
            connection_id=UUID(str(row["connection_id"])),
            project_id=str(row["project_id"]),
            provider=str(row["provider"]),
            credential_version=int(row["credential_version"]),
        )
        del plaintext
    return len(rows), tuple(audit_ids)
