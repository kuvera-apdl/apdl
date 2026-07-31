"""Codegen-owned encrypted project LLM credential lifecycle."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


Provider = Literal["anthropic", "openai", "google", "xai"]
CredentialState = Literal["active", "replaced", "revoked"]

PROVIDERS = frozenset({"anthropic", "openai", "google", "xai"})
ENCRYPTION_KEY_ENV = "CODEGEN_LLM_CREDENTIAL_ENCRYPTION_KEY_BASE64"
ALGORITHM = "AES-256-GCM"
SCHEMA_VERSION = "codegen_llm_provider_credential@1"
SERVICE = "codegen"
REPLACEMENT_REASON = "provider_connection_replaced"
REVOCATION_REASON = "provider_connection_revoked"

_PROJECT_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_ACTOR = re.compile(r"^[^\r\n]{1,512}$")


class CredentialConfigurationError(RuntimeError):
    """The controller credential-encryption configuration is unusable."""


class CredentialStoreError(RuntimeError):
    """A credential operation failed without exposing secret material."""


class CredentialNotFoundError(CredentialStoreError):
    """No matching active credential exists."""


class CredentialConflictError(CredentialStoreError):
    """The requested lifecycle transition lost an optimistic race."""


class CredentialDecryptionError(CredentialStoreError):
    """Stored ciphertext failed authentication."""


@dataclass(frozen=True)
class EncryptedCredential:
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)


@dataclass(frozen=True)
class DecryptedCredential:
    credential_id: UUID
    project_id: str
    provider: Provider
    credential_version: int
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class CredentialMetadata:
    credential_id: UUID
    project_id: str
    provider: Provider
    credential_version: int
    state: CredentialState
    encryption_key_id: str
    created_by_actor: str
    created_at: datetime
    retired_by_actor: str | None
    retirement_reason: str | None
    retired_at: datetime | None


def canonical_provider(provider: str) -> Provider:
    if provider not in PROVIDERS:
        raise ValueError("provider must be anthropic, openai, google, or xai")
    return cast(Provider, provider)


def validate_scope(project_id: str, provider: str) -> Provider:
    if _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("project_id must match ^[A-Za-z0-9]{1,64}$")
    return canonical_provider(provider)


def _validate_actor(actor: str) -> str:
    if actor != actor.strip() or _ACTOR.fullmatch(actor) is None:
        raise ValueError("actor must be 1 to 512 characters without line breaks")
    return actor


class CredentialCipher:
    """Strict AES-256-GCM with Codegen- and credential-bound AAD."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise CredentialConfigurationError(
                "Codegen LLM credential encryption key must decode to exactly 32 bytes"
            )
        self._cipher = AESGCM(key)
        self.key_id = f"sha256:{hashlib.sha256(key).hexdigest()[:32]}"

    @classmethod
    def from_base64(cls, encoded: str) -> "CredentialCipher":
        if (
            not encoded
            or encoded != encoded.strip()
            or not encoded.isascii()
        ):
            raise CredentialConfigurationError(
                "Codegen LLM credential encryption key is missing or malformed"
            )
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialConfigurationError(
                "Codegen LLM credential encryption key is missing or malformed"
            ) from exc
        if base64.b64encode(key).decode("ascii") != encoded:
            raise CredentialConfigurationError(
                "Codegen LLM credential encryption key is missing or malformed"
            )
        return cls(key)

    @classmethod
    def from_environment(cls) -> "CredentialCipher":
        return cls.from_base64(os.getenv(ENCRYPTION_KEY_ENV, ""))

    def _associated_data(
        self,
        credential_id: UUID,
        project_id: str,
        provider: Provider,
    ) -> bytes:
        return json.dumps(
            {
                "credential_id": str(credential_id),
                "encryption_key_id": self.key_id,
                "project_id": project_id,
                "provider": provider,
                "schema": SCHEMA_VERSION,
                "service": SERVICE,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def encrypt(
        self,
        api_key: str,
        *,
        credential_id: UUID,
        project_id: str,
        provider: str,
    ) -> EncryptedCredential:
        canonical = validate_scope(project_id, provider)
        if not api_key or len(api_key.encode("utf-8")) > 16_384:
            raise ValueError("api_key must contain between 1 and 16384 UTF-8 bytes")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            api_key.encode("utf-8"),
            self._associated_data(credential_id, project_id, canonical),
        )
        return EncryptedCredential(ciphertext=ciphertext, nonce=nonce)

    def decrypt(
        self,
        *,
        ciphertext: bytes,
        nonce: bytes,
        credential_id: UUID,
        project_id: str,
        provider: str,
        encryption_key_id: str,
    ) -> str:
        canonical = validate_scope(project_id, provider)
        if encryption_key_id != self.key_id or len(nonce) != 12:
            raise CredentialDecryptionError(
                "Codegen project provider credential could not be authenticated"
            )
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                ciphertext,
                self._associated_data(credential_id, project_id, canonical),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise CredentialDecryptionError(
                "Codegen project provider credential could not be authenticated"
            ) from exc


def _metadata(row: Any) -> CredentialMetadata:
    return CredentialMetadata(
        credential_id=UUID(str(row["credential_id"])),
        project_id=str(row["project_id"]),
        provider=cast(Provider, str(row["provider"])),
        credential_version=int(row["credential_version"]),
        state=cast(CredentialState, str(row["state"])),
        encryption_key_id=str(row["encryption_key_id"]),
        created_by_actor=str(row["created_by_actor"]),
        created_at=row["created_at"],
        retired_by_actor=(
            str(row["retired_by_actor"])
            if row["retired_by_actor"] is not None
            else None
        ),
        retirement_reason=(
            str(row["retirement_reason"])
            if row["retirement_reason"] is not None
            else None
        ),
        retired_at=row["retired_at"],
    )


class ProjectCredentialStore:
    """Lifecycle operations serialized by exact project/provider scope."""

    def __init__(self, pool: Any, cipher: CredentialCipher) -> None:
        self._pool = pool
        self._cipher = cipher

    @property
    def encryption_key_id(self) -> str:
        return self._cipher.key_id

    @staticmethod
    async def lock_pair(conn: Any, project_id: str, provider: str) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"apdl:codegen-llm-credential:{project_id}:{provider}",
        )

    async def create_in_transaction(
        self,
        conn: Any,
        project_id: str,
        provider: str,
        api_key: str,
        *,
        actor: str,
    ) -> CredentialMetadata:
        canonical = validate_scope(project_id, provider)
        actor = _validate_actor(actor)
        await self.lock_pair(conn, project_id, canonical)
        version = await conn.fetchrow(
            """
            SELECT COALESCE(MAX(credential_version), 0) + 1 AS next_version,
                   COALESCE(BOOL_OR(state = 'active'), FALSE) AS active_exists
            FROM codegen_project_provider_credentials
            WHERE project_id = $1 AND provider = $2
            """,
            project_id,
            canonical,
        )
        if bool(version["active_exists"]):
            raise CredentialConflictError(
                "An active Codegen project provider credential already exists"
            )
        credential_id = uuid4()
        encrypted = self._cipher.encrypt(
            api_key,
            credential_id=credential_id,
            project_id=project_id,
            provider=canonical,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO codegen_project_provider_credentials (
                credential_id, project_id, provider, credential_version,
                state, ciphertext, nonce, algorithm, schema_version,
                encryption_key_id, created_by_actor
            ) VALUES (
                $1, $2, $3, $4, 'active', $5, $6,
                'AES-256-GCM', 'codegen_llm_provider_credential@1', $7, $8
            )
            RETURNING *
            """,
            credential_id,
            project_id,
            canonical,
            int(version["next_version"]),
            encrypted.ciphertext,
            encrypted.nonce,
            self._cipher.key_id,
            actor,
        )
        await self._append_audit(
            conn,
            project_id=project_id,
            provider=canonical,
            credential_id=credential_id,
            predecessor_credential_id=None,
            action="create",
            actor=actor,
            encryption_key_id=self._cipher.key_id,
        )
        return _metadata(row)

    async def replace_in_transaction(
        self,
        conn: Any,
        project_id: str,
        provider: str,
        api_key: str,
        *,
        expected_credential_id: UUID,
        actor: str,
    ) -> CredentialMetadata:
        canonical = validate_scope(project_id, provider)
        actor = _validate_actor(actor)
        await self.lock_pair(conn, project_id, canonical)
        current = await conn.fetchrow(
            """
            SELECT credential_id, credential_version
            FROM codegen_project_provider_credentials
            WHERE project_id = $1 AND provider = $2 AND state = 'active'
            FOR UPDATE
            """,
            project_id,
            canonical,
        )
        if (
            current is None
            or UUID(str(current["credential_id"])) != expected_credential_id
        ):
            raise CredentialConflictError(
                "The Codegen project provider credential version changed"
            )
        successor_id = uuid4()
        encrypted = self._cipher.encrypt(
            api_key,
            credential_id=successor_id,
            project_id=project_id,
            provider=canonical,
        )
        await conn.execute(
            """
            UPDATE codegen_project_provider_credentials
            SET state = 'replaced', ciphertext = NULL, nonce = NULL,
                successor_credential_id = $4, retired_by_actor = $5,
                retirement_reason = $6, retired_at = NOW()
            WHERE project_id = $1 AND provider = $2 AND credential_id = $3
            """,
            project_id,
            canonical,
            expected_credential_id,
            successor_id,
            actor,
            REPLACEMENT_REASON,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO codegen_project_provider_credentials (
                credential_id, project_id, provider, credential_version,
                state, ciphertext, nonce, algorithm, schema_version,
                encryption_key_id, created_by_actor
            ) VALUES (
                $1, $2, $3, $4, 'active', $5, $6,
                'AES-256-GCM', 'codegen_llm_provider_credential@1', $7, $8
            )
            RETURNING *
            """,
            successor_id,
            project_id,
            canonical,
            int(current["credential_version"]) + 1,
            encrypted.ciphertext,
            encrypted.nonce,
            self._cipher.key_id,
            actor,
        )
        await self._append_audit(
            conn,
            project_id=project_id,
            provider=canonical,
            credential_id=successor_id,
            predecessor_credential_id=expected_credential_id,
            action="replace",
            actor=actor,
            encryption_key_id=self._cipher.key_id,
        )
        return _metadata(row)

    async def revoke_in_transaction(
        self,
        conn: Any,
        project_id: str,
        provider: str,
        *,
        expected_credential_id: UUID,
        actor: str,
    ) -> CredentialMetadata:
        canonical = validate_scope(project_id, provider)
        actor = _validate_actor(actor)
        await self.lock_pair(conn, project_id, canonical)
        row = await conn.fetchrow(
            """
            UPDATE codegen_project_provider_credentials
            SET state = 'revoked', ciphertext = NULL, nonce = NULL,
                retired_by_actor = $4, retirement_reason = $5,
                retired_at = NOW()
            WHERE project_id = $1 AND provider = $2 AND credential_id = $3
              AND state = 'active'
            RETURNING *
            """,
            project_id,
            canonical,
            expected_credential_id,
            actor,
            REVOCATION_REASON,
        )
        if row is None:
            raise CredentialConflictError(
                "The Codegen project provider credential version changed"
            )
        await self._append_audit(
            conn,
            project_id=project_id,
            provider=canonical,
            credential_id=expected_credential_id,
            predecessor_credential_id=None,
            action="revoke",
            actor=actor,
            encryption_key_id=str(row["encryption_key_id"]),
        )
        return _metadata(row)

    async def create(
        self,
        project_id: str,
        provider: str,
        api_key: str,
        *,
        actor: str,
    ) -> CredentialMetadata:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                return await self.create_in_transaction(
                    conn, project_id, provider, api_key, actor=actor
                )

    async def replace(
        self,
        project_id: str,
        provider: str,
        api_key: str,
        *,
        expected_credential_id: UUID,
        actor: str,
    ) -> CredentialMetadata:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                return await self.replace_in_transaction(
                    conn,
                    project_id,
                    provider,
                    api_key,
                    expected_credential_id=expected_credential_id,
                    actor=actor,
                )

    async def revoke(
        self,
        project_id: str,
        provider: str,
        *,
        expected_credential_id: UUID,
        actor: str,
    ) -> CredentialMetadata:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                return await self.revoke_in_transaction(
                    conn,
                    project_id,
                    provider,
                    expected_credential_id=expected_credential_id,
                    actor=actor,
                )

    async def load_active(
        self,
        project_id: str,
        provider: str,
        *,
        credential_id: UUID | None = None,
    ) -> DecryptedCredential:
        canonical = validate_scope(project_id, provider)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT credential_id, project_id, provider, credential_version,
                       ciphertext, nonce, encryption_key_id
                FROM codegen_project_provider_credentials
                WHERE project_id = $1 AND provider = $2 AND state = 'active'
                  AND ($3::UUID IS NULL OR credential_id = $3)
                """,
                project_id,
                canonical,
                credential_id,
            )
        if row is None:
            raise CredentialNotFoundError(
                "No active Codegen project provider credential exists"
            )
        stored_id = UUID(str(row["credential_id"]))
        api_key = self._cipher.decrypt(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            credential_id=stored_id,
            project_id=project_id,
            provider=canonical,
            encryption_key_id=str(row["encryption_key_id"]),
        )
        return DecryptedCredential(
            credential_id=stored_id,
            project_id=project_id,
            provider=canonical,
            credential_version=int(row["credential_version"]),
            api_key=api_key,
        )

    async def active_metadata(
        self, project_id: str, provider: str
    ) -> CredentialMetadata | None:
        canonical = validate_scope(project_id, provider)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT credential_id, project_id, provider, credential_version,
                       state, encryption_key_id, created_by_actor, created_at,
                       retired_by_actor, retirement_reason, retired_at
                FROM codegen_project_provider_credentials
                WHERE project_id = $1 AND provider = $2 AND state = 'active'
                """,
                project_id,
                canonical,
            )
        return _metadata(row) if row is not None else None

    async def metadata(
        self,
        project_id: str,
        provider: str,
        *,
        credential_id: UUID,
        credential_version: int,
    ) -> CredentialMetadata | None:
        """Return scoped non-secret metadata for one exact historical version."""
        canonical = validate_scope(project_id, provider)
        if credential_version < 1:
            raise ValueError("credential_version must be positive")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT credential_id, project_id, provider, credential_version,
                       state, encryption_key_id, created_by_actor, created_at,
                       retired_by_actor, retirement_reason, retired_at
                FROM codegen_project_provider_credentials
                WHERE project_id = $1
                  AND provider = $2
                  AND credential_id = $3
                  AND credential_version = $4
                """,
                project_id,
                canonical,
                credential_id,
                credential_version,
            )
        return _metadata(row) if row is not None else None

    @staticmethod
    async def _append_audit(
        conn: Any,
        *,
        project_id: str,
        provider: Provider,
        credential_id: UUID,
        predecessor_credential_id: UUID | None,
        action: str,
        actor: str,
        encryption_key_id: str,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO codegen_project_provider_credential_audit (
                project_id, provider, credential_id,
                predecessor_credential_id, action, outcome, actor,
                encryption_key_id
            ) VALUES ($1, $2, $3, $4, $5, 'succeeded', $6, $7)
            """,
            project_id,
            provider,
            credential_id,
            predecessor_credential_id,
            action,
            actor,
            encryption_key_id,
        )


async def rotate_active_credentials(
    conn: Any,
    *,
    old_cipher: CredentialCipher,
    new_cipher: CredentialCipher,
    actor: str,
) -> tuple[int, tuple[UUID, ...]]:
    """Re-encrypt every active Codegen credential under an exclusive barrier."""
    actor = _validate_actor(actor)
    if old_cipher.key_id == new_cipher.key_id:
        raise ValueError("The new encryption key must differ from the old key")
    is_in_transaction = getattr(conn, "is_in_transaction", None)
    if not callable(is_in_transaction) or is_in_transaction() is not True:
        raise CredentialStoreError(
            "Active PostgreSQL transaction is required for credential rotation"
        )
    held = await conn.fetchval(
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
    if held is not True:
        raise CredentialStoreError(
            "Exclusive PostgreSQL maintenance barrier is required for rotation"
        )
    rows = await conn.fetch(
        """
        SELECT credential_id, project_id, provider, ciphertext, nonce,
               encryption_key_id
        FROM codegen_project_provider_credentials
        WHERE state = 'active'
        ORDER BY project_id, provider
        FOR UPDATE
        """
    )
    # Authenticate every row under the old key before the first write so a
    # wrong or partially stale key rolls the entire transaction back without
    # changing any credential. Do not retain the returned plaintext: active
    # projects can be numerous, and rotation must never aggregate their
    # provider secrets in process memory.
    for row in rows:
        plaintext = old_cipher.decrypt(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            credential_id=UUID(str(row["credential_id"])),
            project_id=str(row["project_id"]),
            provider=str(row["provider"]),
            encryption_key_id=str(row["encryption_key_id"]),
        )
        del plaintext

    audit_ids: list[UUID] = []
    for row in rows:
        credential_id = UUID(str(row["credential_id"]))
        plaintext = old_cipher.decrypt(
            ciphertext=bytes(row["ciphertext"]),
            nonce=bytes(row["nonce"]),
            credential_id=credential_id,
            project_id=str(row["project_id"]),
            provider=str(row["provider"]),
            encryption_key_id=str(row["encryption_key_id"]),
        )
        try:
            encrypted = new_cipher.encrypt(
                plaintext,
                credential_id=credential_id,
                project_id=str(row["project_id"]),
                provider=str(row["provider"]),
            )
        finally:
            del plaintext
        await conn.execute(
            """
            UPDATE codegen_project_provider_credentials
            SET ciphertext = $2, nonce = $3, encryption_key_id = $4
            WHERE credential_id = $1 AND state = 'active'
            """,
            credential_id,
            encrypted.ciphertext,
            encrypted.nonce,
            new_cipher.key_id,
        )
        audit_id = uuid4()
        await conn.execute(
            """
            INSERT INTO codegen_project_provider_credential_audit (
                audit_id, project_id, provider, credential_id, action,
                outcome, actor, encryption_key_id
            ) VALUES ($1, $2, $3, $4, 'reencrypt', 'succeeded', $5, $6)
            """,
            audit_id,
            str(row["project_id"]),
            str(row["provider"]),
            credential_id,
            actor,
            new_cipher.key_id,
        )
        audit_ids.append(audit_id)
    for row in rows:
        credential_id = UUID(str(row["credential_id"]))
        stored = await conn.fetchrow(
            """
            SELECT ciphertext, nonce, encryption_key_id
            FROM codegen_project_provider_credentials
            WHERE credential_id = $1 AND state = 'active'
            """,
            credential_id,
        )
        if stored is None:
            raise CredentialStoreError(
                "Codegen credential disappeared during key rotation"
            )
        plaintext = new_cipher.decrypt(
            ciphertext=bytes(stored["ciphertext"]),
            nonce=bytes(stored["nonce"]),
            credential_id=credential_id,
            project_id=str(row["project_id"]),
            provider=str(row["provider"]),
            encryption_key_id=str(stored["encryption_key_id"]),
        )
        del plaintext
    return len(rows), tuple(audit_ids)
