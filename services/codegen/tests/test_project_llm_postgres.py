"""Live PostgreSQL checks for Codegen project-scoped LLM authority.

Set ``CODEGEN_TEST_POSTGRES_URL`` to a disposable, fully migrated database
owned by an operator role. The regular unit suite ignores this module; CI runs
it explicitly and refuses to skip it.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app.editor.environment import codegen_tenant_behavior_configuration_sha256
from app.llm.provider_catalog import catalog_model
from app.models.execution import PublicationStage, RiskLevel
from app.publication import (
    DEVELOPMENT_CODEGEN_REVISION,
    build_development_publication_authorization,
)
from app.store.llm_connections import (
    LlmConnectionConflictError,
    ProjectConnectionStore,
)
from app.store.llm_credentials import (
    CredentialCipher,
    CredentialConflictError,
    CredentialDecryptionError,
    CredentialNotFoundError,
    CredentialMetadata,
    CredentialStoreError,
    ProjectCredentialStore,
    rotate_active_credentials,
)
from app.store.llm_routing import (
    LlmRoutingUnavailableError,
    assign_project_models,
    prepare_llm_attempt,
)


POSTGRES_URL = os.getenv("CODEGEN_TEST_POSTGRES_URL", "").strip() or None

if os.getenv("GITHUB_ACTIONS") == "true" and POSTGRES_URL is None:
    raise RuntimeError(
        "CODEGEN_TEST_POSTGRES_URL is required in GitHub Actions; "
        "the Codegen project LLM PostgreSQL suite must not be skipped"
    )

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="CODEGEN_TEST_POSTGRES_URL is not configured",
)

CIPHER = CredentialCipher(bytes(range(32)))
ROTATED_CIPHER = CredentialCipher(b"n" * 32)
MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


class KnownRotationSourceCipher(CredentialCipher):
    """Test-only source able to re-run against rows from a prior live run."""

    def __init__(self) -> None:
        super().__init__(bytes(range(32)))

    def decrypt(self, **kwargs: Any) -> str:
        key_id = kwargs.get("encryption_key_id")
        source = ROTATED_CIPHER if key_id == ROTATED_CIPHER.key_id else CIPHER
        return source.decrypt(**kwargs)


def _project_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


async def _create_operator_project(
    conn: asyncpg.Connection,
    project_id: str,
    *,
    with_owner: bool = False,
) -> uuid.UUID | None:
    await conn.execute(
        "INSERT INTO admin_projects (project_id) VALUES ($1)",
        project_id,
    )
    if not with_owner:
        return None
    owner_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO admin_users (
            user_id, email, password_hash, active
        ) VALUES ($1, $2, '$argon2id$codegen-postgres-fixture', TRUE)
        """,
        owner_id,
        f"{owner_id.hex}@codegen.test",
    )
    await conn.execute(
        """
        INSERT INTO admin_user_projects (user_id, project_id, roles)
        VALUES (
            $1, $2,
            ARRAY[
                'agents:read', 'agents:manage', 'credentials:manage',
                'members:manage'
            ]::TEXT[]
        )
        """,
        owner_id,
        project_id,
    )
    await conn.execute(
        "UPDATE admin_projects SET owner_user_id = $2 WHERE project_id = $1",
        project_id,
        owner_id,
    )
    return owner_id


@pytest.mark.asyncio
async def test_credential_lifecycle_serializes_and_never_stores_plaintext() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=4)
    project_id = _project_id("cred")
    other_project_id = _project_id("other")
    first_secret = f"first-{uuid.uuid4().hex}"
    second_secret = f"second-{uuid.uuid4().hex}"
    store = ProjectCredentialStore(pool, CIPHER)
    try:
        async with pool.acquire() as conn:
            await _create_operator_project(conn, project_id)
            await _create_operator_project(conn, other_project_id)

        results = await asyncio.gather(
            store.create(
                project_id,
                "openai",
                first_secret,
                actor="test:credential-create-a",
            ),
            store.create(
                project_id,
                "openai",
                second_secret,
                actor="test:credential-create-b",
            ),
            return_exceptions=True,
        )
        created = [
            value for value in results if isinstance(value, CredentialMetadata)
        ]
        conflicts = [
            value
            for value in results
            if isinstance(value, CredentialConflictError)
        ]
        assert len(created) == 1
        assert len(conflicts) == 1
        active = created[0]
        winning_secret = (
            first_secret
            if (await store.load_active(project_id, "openai")).api_key
            == first_secret
            else second_secret
        )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ciphertext, nonce
                FROM codegen_project_provider_credentials
                WHERE credential_id = $1
                """,
                active.credential_id,
            )
        assert row is not None
        ciphertext = bytes(row["ciphertext"])
        assert winning_secret.encode() not in ciphertext
        assert first_secret.encode() not in ciphertext
        assert second_secret.encode() not in ciphertext
        assert len(bytes(row["nonce"])) == 12
        async with pool.acquire() as conn:
            with pytest.raises(
                asyncpg.CheckViolationError,
                match="successor_not_self",
            ):
                await conn.execute(
                    """
                    UPDATE codegen_project_provider_credentials
                    SET state = 'replaced', ciphertext = NULL, nonce = NULL,
                        successor_credential_id = credential_id,
                        retired_by_actor = 'test:self-loop',
                        retirement_reason = 'provider_connection_replaced',
                        retired_at = NOW()
                    WHERE credential_id = $1
                    """,
                    active.credential_id,
                )

        replacement = await store.replace(
            project_id,
            "openai",
            f"replacement-{uuid.uuid4().hex}",
            expected_credential_id=active.credential_id,
            actor="test:credential-replace",
        )
        historical = await store.metadata(
            project_id,
            "openai",
            credential_id=active.credential_id,
            credential_version=active.credential_version,
        )
        assert historical is not None
        assert historical.state == "replaced"
        assert replacement.credential_version == active.credential_version + 1
        with pytest.raises(CredentialNotFoundError):
            await store.load_active(other_project_id, "openai")
        with pytest.raises(CredentialNotFoundError):
            await store.load_active(
                project_id,
                "openai",
                credential_id=active.credential_id,
            )

        revoked = await store.revoke(
            project_id,
            "openai",
            expected_credential_id=replacement.credential_id,
            actor="test:credential-revoke",
        )
        assert revoked.state == "revoked"
        with pytest.raises(CredentialNotFoundError):
            await store.load_active(project_id, "openai")

        async with pool.acquire() as conn:
            lifecycle = await conn.fetch(
                """
                SELECT state, ciphertext, nonce, retirement_reason
                FROM codegen_project_provider_credentials
                WHERE project_id = $1 AND provider = 'openai'
                ORDER BY credential_version
                """,
                project_id,
            )
            audit = await conn.fetch(
                """
                SELECT action
                FROM codegen_project_provider_credential_audit
                WHERE project_id = $1
                ORDER BY created_at, audit_id
                """,
                project_id,
            )
        assert [
            (
                row["state"],
                row["ciphertext"],
                row["nonce"],
                row["retirement_reason"],
            )
            for row in lifecycle
        ] == [
            ("replaced", None, None, "provider_connection_replaced"),
            ("revoked", None, None, "provider_connection_revoked"),
        ]
        assert [row["action"] for row in audit] == [
            "create",
            "replace",
            "revoke",
        ]
        async with pool.acquire() as conn:
            for state, wrong_reason in (
                ("replaced", "provider_connection_revoked"),
                ("revoked", "provider_connection_replaced"),
            ):
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_credentials
                        SET retirement_reason = $3
                        WHERE project_id = $1
                          AND provider = 'openai'
                          AND state = $2
                        """,
                        project_id,
                        state,
                        wrong_reason,
                    )
            with pytest.raises(
                asyncpg.CheckViolationError,
                match="audit rows are immutable",
            ):
                await conn.execute(
                    """
                    UPDATE codegen_project_provider_credential_audit
                    SET actor = 'test:mutated-audit'
                    WHERE project_id = $1
                    """,
                    project_id,
                )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_connection_mutations_are_authorized_and_optimistic() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=4)
    project_id = _project_id("conn")
    credentials = ProjectCredentialStore(pool, CIPHER)
    connections = ProjectConnectionStore(pool, credentials)
    model = catalog_model("openai", "gpt-5.4-mini")
    assert model is not None
    try:
        async with pool.acquire() as conn:
            owner_id = await _create_operator_project(
                conn,
                project_id,
                with_owner=True,
            )
        assert owner_id is not None

        results = await asyncio.gather(
            connections.put(
                project_id,
                "openai",
                f"connection-a-{uuid.uuid4().hex}",
                (model,),
                expected_version=0,
                actor_user_id=owner_id,
            ),
            connections.put(
                project_id,
                "openai",
                f"connection-b-{uuid.uuid4().hex}",
                (model,),
                expected_version=0,
                actor_user_id=owner_id,
            ),
            return_exceptions=True,
        )
        connected = [
            value
            for value in results
            if not isinstance(value, BaseException)
        ]
        conflicts = [
            value
            for value in results
            if isinstance(value, LlmConnectionConflictError)
        ]
        assert len(connected) == 1
        assert len(conflicts) == 1
        assert connected[0].version == 1
        assert connected[0].inventory_version == 1

        metadata, inventory = await connections.get_active_with_models(
            project_id,
            "openai",
        )
        assert metadata.model_count == 1
        assert inventory == (model,)
        refreshed = await connections.refresh(
            project_id,
            "openai",
            (model,),
            expected_version=1,
            expected_credential_id=metadata.credential_id,
            actor_user_id=owner_id,
        )
        assert (refreshed.version, refreshed.inventory_version) == (2, 2)
        with pytest.raises(LlmConnectionConflictError):
            await connections.refresh(
                project_id,
                "openai",
                (model,),
                expected_version=1,
                expected_credential_id=metadata.credential_id,
                actor_user_id=owner_id,
            )
        revoked = await connections.revoke(
            project_id,
            "openai",
            expected_version=2,
            actor_user_id=owner_id,
        )
        assert (revoked.state, revoked.version, revoked.model_count) == (
            "revoked",
            3,
            0,
        )
        reconnected = await connections.put(
            project_id,
            "openai",
            f"connection-reconnected-{uuid.uuid4().hex}",
            (model,),
            expected_version=3,
            actor_user_id=owner_id,
        )
        assert (reconnected.state, reconnected.version) == ("active", 4)
        assert reconnected.credential_id != metadata.credential_id

        async with pool.acquire() as conn:
            credential = await conn.fetchrow(
                """
                SELECT state, ciphertext, nonce
                FROM codegen_project_provider_credentials
                WHERE credential_id = $1
                """,
                metadata.credential_id,
            )
            reconnected_credential_version = await conn.fetchval(
                """
                SELECT credential_version
                FROM codegen_project_provider_credentials
                WHERE credential_id = $1
                """,
                reconnected.credential_id,
            )
            actions = await conn.fetch(
                """
                SELECT action
                FROM codegen_project_provider_connection_audit
                WHERE project_id = $1
                ORDER BY created_at, audit_id
                """,
                project_id,
            )
        assert dict(credential) == {
            "state": "revoked",
            "ciphertext": None,
            "nonce": None,
        }
        assert reconnected_credential_version == 2
        assert [row["action"] for row in actions] == [
            "connect",
            "refresh",
            "revoke",
            "connect",
        ]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_model_inventory_and_assignments_keep_exact_database_provenance() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    project_id = _project_id("provenance")
    credentials = ProjectCredentialStore(pool, CIPHER)
    connections = ProjectConnectionStore(pool, credentials)
    model = catalog_model("openai", "gpt-5.4-mini")
    assert model is not None
    try:
        async with pool.acquire() as conn:
            owner_id = await _create_operator_project(
                conn,
                project_id,
                with_owner=True,
            )
        assert owner_id is not None
        connected = await connections.put(
            project_id,
            "openai",
            f"provenance-{uuid.uuid4().hex}",
            (model,),
            expected_version=0,
            actor_user_id=owner_id,
        )

        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_models
                        SET catalog_version = 'codegen-provider-catalog@2'
                        WHERE project_id = $1 AND provider = 'openai'
                        """,
                        project_id,
                    )
            with pytest.raises(
                asyncpg.CheckViolationError,
                match="must not retain model inventory",
            ):
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_connections
                        SET state = 'revoked', revoked_at = NOW()
                        WHERE project_id = $1 AND provider = 'openai'
                        """,
                        project_id,
                    )

        assignments = await assign_project_models(
            pool,
            project_id=project_id,
            editor_provider="openai",
            editor_model_id=model.model_id,
            helper_provider="openai",
            helper_model_id=model.model_id,
            actor="test:model-provenance",
        )
        assert tuple(item.role for item in assignments) == ("editor", "helper")
        refreshed = await connections.refresh(
            project_id,
            "openai",
            (model,),
            expected_version=connected.version,
            expected_credential_id=connected.credential_id,
            actor_user_id=owner_id,
        )
        assert (refreshed.version, refreshed.inventory_version) == (2, 2)

        async with pool.acquire() as conn:
            refreshed_assignments = await conn.fetch(
                """
                SELECT role, assignment_version, connection_version,
                       inventory_version, catalog_version
                FROM codegen_project_model_assignments
                WHERE project_id = $1
                ORDER BY role
                """,
                project_id,
            )
            assert [tuple(row.values()) for row in refreshed_assignments] == [
                ("editor", 2, 2, 2, "codegen-provider-catalog@1"),
                ("helper", 2, 2, 2, "codegen-provider-catalog@1"),
            ]
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE codegen_project_model_assignments
                        SET catalog_version = 'codegen-provider-catalog@2'
                        WHERE project_id = $1 AND role = 'editor'
                        """,
                        project_id,
                    )
            with pytest.raises(
                asyncpg.CheckViolationError,
                match="stale model assignment",
            ):
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_models
                        SET supported_roles = ARRAY['helper']::TEXT[]
                        WHERE project_id = $1 AND provider = 'openai'
                        """,
                        project_id,
                    )
            with pytest.raises(
                (
                    asyncpg.CheckViolationError,
                    asyncpg.ForeignKeyViolationError,
                )
            ):
                async with conn.transaction():
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_connections
                        SET version = 3,
                            inventory_version = 3,
                            catalog_version = 'codegen-provider-catalog@2'
                        WHERE project_id = $1 AND provider = 'openai'
                        """,
                        project_id,
                    )
                    await conn.execute(
                        """
                        UPDATE codegen_project_provider_models
                        SET connection_version = 3,
                            inventory_version = 3,
                            catalog_version = 'codegen-provider-catalog@2'
                        WHERE project_id = $1 AND provider = 'openai'
                        """,
                        project_id,
                    )

            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE codegen_project_provider_connections
                    SET version = 3,
                        inventory_version = 3,
                        catalog_version = 'codegen-provider-catalog@2'
                    WHERE project_id = $1 AND provider = 'openai'
                    """,
                    project_id,
                )
                await conn.execute(
                    """
                    UPDATE codegen_project_provider_models
                    SET connection_version = 3,
                        inventory_version = 3,
                        catalog_version = 'codegen-provider-catalog@2'
                    WHERE project_id = $1 AND provider = 'openai'
                    """,
                    project_id,
                )
                await conn.execute(
                    """
                    UPDATE codegen_project_model_assignments
                    SET assignment_version = assignment_version + 1,
                        connection_version = 3,
                        inventory_version = 3,
                        catalog_version = 'codegen-provider-catalog@2'
                    WHERE project_id = $1 AND provider = 'openai'
                    """,
                    project_id,
                )
            provenance = await conn.fetch(
                """
                SELECT role, assignment_version, connection_version,
                       inventory_version, catalog_version
                FROM codegen_project_model_assignments
                WHERE project_id = $1
                ORDER BY role
                """,
                project_id,
            )
        assert [tuple(row.values()) for row in provenance] == [
            ("editor", 3, 3, 3, "codegen-provider-catalog@2"),
            ("helper", 3, 3, 3, "codegen-provider-catalog@2"),
        ]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_key_rotation_requires_barriers_and_reencrypts_atomically() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    new_cipher = ROTATED_CIPHER
    source_cipher = KnownRotationSourceCipher()
    old_store = ProjectCredentialStore(pool, CIPHER)
    projects = (_project_id("rotatea"), _project_id("rotateb"))
    secrets = (
        f"rotation-secret-a-{uuid.uuid4().hex}",
        f"rotation-secret-b-{uuid.uuid4().hex}",
    )
    try:
        async with pool.acquire() as setup:
            for project_id in projects:
                await _create_operator_project(setup, project_id)
        first = await old_store.create(
            projects[0],
            "anthropic",
            secrets[0],
            actor="test:rotation-setup",
        )
        second = await old_store.create(
            projects[1],
            "google",
            secrets[1],
            actor="test:rotation-setup",
        )

        conn = await asyncpg.connect(POSTGRES_URL)
        try:
            with pytest.raises(
                CredentialStoreError,
                match="Active PostgreSQL transaction",
            ):
                await rotate_active_credentials(
                    conn,
                    old_cipher=source_cipher,
                    new_cipher=new_cipher,
                    actor="test:key-rotation",
                )
            with pytest.raises(
                CredentialStoreError,
                match="maintenance barrier",
            ):
                async with conn.transaction():
                    await rotate_active_credentials(
                        conn,
                        old_cipher=source_cipher,
                        new_cipher=new_cipher,
                        actor="test:key-rotation",
                    )
            await conn.execute(
                "SELECT pg_advisory_lock($1::BIGINT)",
                MAINTENANCE_INHIBITOR_LOCK_ID,
            )
            with pytest.raises(
                CredentialStoreError,
                match="maintenance barrier",
            ):
                async with conn.transaction():
                    await rotate_active_credentials(
                        conn,
                        old_cipher=CIPHER,
                        new_cipher=new_cipher,
                        actor="test:key-rotation",
                    )
            await conn.execute(
                "SELECT pg_advisory_lock($1::BIGINT)",
                MAINTENANCE_GUARD_LOCK_ID,
            )
            async with conn.transaction():
                count, audit_ids = await rotate_active_credentials(
                    conn,
                    old_cipher=source_cipher,
                    new_cipher=new_cipher,
                    actor="test:key-rotation",
                )
        finally:
            await conn.close()

        assert count >= 2
        assert len(audit_ids) == count
        new_store = ProjectCredentialStore(pool, new_cipher)
        assert (
            await new_store.load_active(projects[0], "anthropic")
        ).api_key == secrets[0]
        assert (
            await new_store.load_active(projects[1], "google")
        ).api_key == secrets[1]
        with pytest.raises(CredentialDecryptionError):
            await old_store.load_active(projects[0], "anthropic")

        async with pool.acquire() as verify:
            rows = await verify.fetch(
                """
                SELECT credential_id, encryption_key_id
                FROM codegen_project_provider_credentials
                WHERE credential_id = ANY($1::UUID[])
                ORDER BY credential_id
                """,
                [first.credential_id, second.credential_id],
            )
            audited = await verify.fetchval(
                """
                SELECT count(*)
                FROM codegen_project_provider_credential_audit
                WHERE audit_id = ANY($1::UUID[])
                  AND action = 'reencrypt'
                  AND encryption_key_id = $2
                """,
                list(audit_ids),
                new_cipher.key_id,
            )
        assert len(rows) == 2
        assert {
            row["encryption_key_id"] for row in rows
        } == {new_cipher.key_id}
        assert audited == count
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_key_rotation_rolls_back_after_mid_write_failure() -> None:
    assert POSTGRES_URL is not None
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=3)
    old_store = ProjectCredentialStore(pool, CIPHER)
    projects = (_project_id("rollbacka"), _project_id("rollbackb"))
    credentials: list[CredentialMetadata] = []

    class FailingCipher(CredentialCipher):
        def __init__(self) -> None:
            super().__init__(b"f" * 32)
            self.encryptions = 0

        def encrypt(self, *args: Any, **kwargs: Any):
            self.encryptions += 1
            if self.encryptions == 2:
                raise RuntimeError("injected rotation failure")
            return super().encrypt(*args, **kwargs)

    try:
        async with pool.acquire() as setup:
            for project_id in projects:
                await _create_operator_project(setup, project_id)
        for index, project_id in enumerate(projects):
            credentials.append(
                await old_store.create(
                    project_id,
                    "xai",
                    f"rollback-secret-{index}-{uuid.uuid4().hex}",
                    actor="test:rotation-rollback-setup",
                )
            )

        conn = await asyncpg.connect(POSTGRES_URL)
        failing_cipher = FailingCipher()
        try:
            await conn.execute(
                "SELECT pg_advisory_lock($1::BIGINT)",
                MAINTENANCE_INHIBITOR_LOCK_ID,
            )
            await conn.execute(
                "SELECT pg_advisory_lock($1::BIGINT)",
                MAINTENANCE_GUARD_LOCK_ID,
            )
            with pytest.raises(RuntimeError, match="injected rotation failure"):
                async with conn.transaction():
                    await rotate_active_credentials(
                        conn,
                        old_cipher=KnownRotationSourceCipher(),
                        new_cipher=failing_cipher,
                        actor="test:key-rotation-rollback",
                    )
        finally:
            await conn.close()

        async with pool.acquire() as verify:
            rows = await verify.fetch(
                """
                SELECT credential_id, encryption_key_id
                FROM codegen_project_provider_credentials
                WHERE credential_id = ANY($1::UUID[])
                ORDER BY credential_id
                """,
                [item.credential_id for item in credentials],
            )
            audit_count = await verify.fetchval(
                """
                SELECT count(*)
                FROM codegen_project_provider_credential_audit
                WHERE credential_id = ANY($1::UUID[])
                  AND action = 'reencrypt'
                """,
                [item.credential_id for item in credentials],
            )
        assert len(rows) == 2
        assert {row["encryption_key_id"] for row in rows} == {CIPHER.key_id}
        assert audit_count == 0
        for project_id in projects:
            assert (
                await old_store.load_active(project_id, "xai")
            ).credential_version == 1
    finally:
        await pool.close()


def _assignment(role: str) -> dict[str, Any]:
    return {
        "schema_version": "codegen_llm_assignment_snapshot@1",
        "role": role,
        "provider": "openai",
        "model_id": "gpt-5.4-mini",
        "assignment_version": 1,
        "connection_version": 1,
        "inventory_version": 1,
        "catalog_version": "codegen-provider-catalog@1",
        "context_window_tokens": 400_000,
        "supports_tool_calling": True,
        "supports_structured_output": True,
        "input_cost_per_million_tokens_usd_micros": 250_000,
        "output_cost_per_million_tokens_usd_micros": 2_000_000,
    }


def _snapshot(
    project_id: str,
    grant_id: str,
    repository_id: int,
    installation_id: int,
    repository_full_name: str,
) -> dict[str, Any]:
    return {
        "schema_version": "codegen_llm_execution_snapshot@2",
        "project_id": project_id,
        "repository_grant_id": grant_id,
        "repository_id": repository_id,
        "repository_installation_id": installation_id,
        "repository_full_name": repository_full_name,
        "codegen_revision": "live-postgres-test",
        "behavior_configuration_sha256": "a" * 64,
        "rollout_stage": PublicationStage.offline.value,
        "assignments": [_assignment("editor"), _assignment("helper")],
    }


async def _insert_changeset(
    conn: asyncpg.Connection,
    *,
    changeset_id: str,
    project_id: str,
    grant_id: str,
    repository_id: int,
    installation_id: int,
    repository_full_name: str,
    snapshot: dict[str, Any] | str,
) -> None:
    encoded_snapshot = (
        snapshot
        if isinstance(snapshot, str)
        else json.dumps(snapshot)
    )
    await conn.execute(
        """
        INSERT INTO codegen_changesets (
            changeset_id, project_id, status, task,
            repository_grant_id, repository_id,
            repository_installation_id, repository_full_name,
            idempotency_key, idempotency_request_sha256,
            llm_execution_snapshot
        ) VALUES (
            $1, $2, 'editing', '{"context":{}}'::JSONB,
            $3, $4, $5, $6, $7, $8, $9::JSONB
        )
        """,
        changeset_id,
        project_id,
        grant_id,
        repository_id,
        installation_id,
        repository_full_name,
        f"live:{changeset_id}",
        uuid.uuid4().hex + uuid.uuid4().hex,
        encoded_snapshot,
    )


@pytest.mark.asyncio
async def test_snapshot_and_attempt_constraints_are_enforced_by_postgres() -> None:
    assert POSTGRES_URL is not None
    conn = await asyncpg.connect(POSTGRES_URL)
    project_id = _project_id("route")
    grant_id = f"ghg_{uuid.uuid4().hex}"
    repository_id = uuid.uuid4().int % 1_000_000_000 + 1
    installation_id = uuid.uuid4().int % 1_000_000_000 + 1
    repository_full_name = f"apdl/{project_id}"
    changeset_id = f"changeset-{uuid.uuid4().hex}"
    try:
        migrated = await conn.fetchrow(
            """
            SELECT
                to_regclass(
                    'public.codegen_project_provider_credentials'
                )::TEXT AS credentials,
                to_regclass(
                    'public.codegen_project_provider_connections'
                )::TEXT AS connections,
                to_regclass('public.codegen_llm_attempts')::TEXT AS attempts
            """
        )
        assert dict(migrated) == {
            "credentials": "codegen_project_provider_credentials",
            "connections": "codegen_project_provider_connections",
            "attempts": "codegen_llm_attempts",
        }
        await _create_operator_project(conn, project_id)
        await conn.execute(
            """
            INSERT INTO github_repository_grants (
                grant_id, project_id, installation_id, repository_id,
                repository_full_name, status, authorization_source,
                authorization_subject, verified_at
            ) VALUES (
                $1, $2, $3, $4, $5, 'active', 'operator',
                'test:codegen-project-llm-postgres', NOW()
            )
            """,
            grant_id,
            project_id,
            installation_id,
            repository_id,
            repository_full_name,
        )
        await conn.execute(
            """
            INSERT INTO codegen_connections (project_id, grant_id)
            VALUES ($1, $2)
            """,
            project_id,
            grant_id,
        )
        snapshot = _snapshot(
            project_id,
            grant_id,
            repository_id,
            installation_id,
            repository_full_name,
        )
        await _insert_changeset(
            conn,
            changeset_id=changeset_id,
            project_id=project_id,
            grant_id=grant_id,
            repository_id=repository_id,
            installation_id=installation_id,
            repository_full_name=repository_full_name,
            snapshot=snapshot,
        )

        malformed = json.loads(json.dumps(snapshot))
        malformed["assignments"][0]["model_id"] = 42
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_changeset(
                conn,
                changeset_id=f"malformed-{uuid.uuid4().hex}",
                project_id=project_id,
                grant_id=grant_id,
                repository_id=repository_id,
                installation_id=installation_id,
                repository_full_name=repository_full_name,
                snapshot=malformed,
            )
        malformed_top_level = json.loads(json.dumps(snapshot))
        malformed_top_level["codegen_revision"] = 54
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_changeset(
                conn,
                changeset_id=f"numeric-top-level-{uuid.uuid4().hex}",
                project_id=project_id,
                grant_id=grant_id,
                repository_id=repository_id,
                installation_id=installation_id,
                repository_full_name=repository_full_name,
                snapshot=malformed_top_level,
            )
        unnormalized_revision = json.loads(json.dumps(snapshot))
        unnormalized_revision["codegen_revision"] = " live-postgres-test"
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_changeset(
                conn,
                changeset_id=f"unnormalized-revision-{uuid.uuid4().hex}",
                project_id=project_id,
                grant_id=grant_id,
                repository_id=repository_id,
                installation_id=installation_id,
                repository_full_name=repository_full_name,
                snapshot=unnormalized_revision,
            )
        numeric_digest = json.loads(json.dumps(snapshot))
        numeric_digest["behavior_configuration_sha256"] = int("1" * 64)
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_changeset(
                conn,
                changeset_id=f"numeric-digest-{uuid.uuid4().hex}",
                project_id=project_id,
                grant_id=grant_id,
                repository_id=repository_id,
                installation_id=installation_id,
                repository_full_name=repository_full_name,
                snapshot=numeric_digest,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            async with conn.transaction():
                await conn.execute("SET LOCAL session_replication_role = replica")
                await conn.execute(
                    """
                    INSERT INTO codegen_changesets (
                        changeset_id, project_id, status, task,
                        repository_target_quarantined,
                        idempotency_key, idempotency_request_sha256,
                        llm_execution_snapshot
                    ) VALUES (
                        $1, $2, 'error', '{"context":{}}'::JSONB, TRUE,
                        $3, $4, $5::JSONB
                    )
                    """,
                    f"null-target-{uuid.uuid4().hex}",
                    project_id,
                    f"live:null-target-{uuid.uuid4().hex}",
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    json.dumps(snapshot),
                )
        for number in ("1.2", "1.2e0"):
            encoded = json.dumps(
                snapshot,
                separators=(",", ":"),
            ).replace(
                '"assignment_version":1',
                f'"assignment_version":{number}',
                1,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_changeset(
                    conn,
                    changeset_id=f"noninteger-{uuid.uuid4().hex}",
                    project_id=project_id,
                    grant_id=grant_id,
                    repository_id=repository_id,
                    installation_id=installation_id,
                    repository_full_name=repository_full_name,
                    snapshot=encoded,
                )
        changed = json.loads(json.dumps(snapshot))
        changed["codegen_revision"] = "changed-after-admission"
        with pytest.raises(
            asyncpg.CheckViolationError,
            match="execution snapshot is immutable",
        ):
            await conn.execute(
                """
                UPDATE codegen_changesets
                SET llm_execution_snapshot = $2::JSONB
                WHERE changeset_id = $1
                """,
                changeset_id,
                json.dumps(changed),
            )

        attempt_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO codegen_llm_attempts (
                attempt_id, project_id, changeset_id, phase, role,
                attempt_sequence, provider, model_id, assignment_version,
                status, finished_at, error_classification
            ) VALUES (
                $1, $2, $3, 'brief', 'helper', 1, 'openai',
                'gpt-5.4-mini', 1, 'blocked', NOW(),
                'changeset_unavailable'
            )
            """,
            attempt_id,
            project_id,
            changeset_id,
        )
        for provider, model_id, assignment_version in (
            ("anthropic", "gpt-5.4-mini", 1),
            ("openai", "gpt-5.4-nano", 1),
            ("openai", "gpt-5.4-mini", 2),
        ):
            with pytest.raises(
                asyncpg.CheckViolationError,
                match="must match its immutable execution snapshot",
            ):
                await conn.execute(
                    """
                    INSERT INTO codegen_llm_attempts (
                        project_id, changeset_id, phase, role,
                        attempt_sequence, provider, model_id,
                        assignment_version, status, finished_at,
                        error_classification
                    ) VALUES (
                        $1, $2, 'brief', 'helper', 2, $3, $4, $5,
                        'blocked', NOW(), 'changeset_unavailable'
                    )
                    """,
                    project_id,
                    changeset_id,
                    provider,
                    model_id,
                    assignment_version,
                )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO codegen_llm_attempts (
                    project_id, changeset_id, phase, role, attempt_sequence,
                    provider, model_id, assignment_version, status
                ) VALUES (
                    $1, $2, 'edit', 'editor', 1, 'openai',
                    'gpt-5.4-mini', 1, 'prepared'
                )
                """,
                project_id,
                changeset_id,
            )

        executable_credential_id = uuid.uuid4()
        executable_secret = f"attempt-secret-{uuid.uuid4().hex}"
        encrypted = CIPHER.encrypt(
            executable_secret,
            credential_id=executable_credential_id,
            project_id=project_id,
            provider="openai",
        )
        await conn.execute(
            """
            INSERT INTO codegen_project_provider_credentials (
                credential_id, project_id, provider, credential_version,
                state, ciphertext, nonce, algorithm, schema_version,
                encryption_key_id, created_by_actor
            ) VALUES (
                $1, $2, 'openai', 1, 'active', $3, $4, 'AES-256-GCM',
                'codegen_llm_provider_credential@1', $5,
                'test:attempt-credential'
            )
            """,
            executable_credential_id,
            project_id,
            encrypted.ciphertext,
            encrypted.nonce,
            CIPHER.key_id,
        )
        executable_attempt_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO codegen_llm_attempts (
                attempt_id, project_id, changeset_id, phase, role,
                attempt_sequence, provider, model_id, assignment_version,
                credential_id, credential_version, status
            ) VALUES (
                $1, $2, $3, 'edit', 'editor', 1, 'openai',
                'gpt-5.4-mini', 1, $4, 1, 'prepared'
            )
            """,
            executable_attempt_id,
            project_id,
            changeset_id,
            executable_credential_id,
        )
        await conn.execute(
            """
            UPDATE codegen_llm_attempts
            SET status = 'in_flight', egress_at = NOW()
            WHERE attempt_id = $1
            """,
            executable_attempt_id,
        )
        await conn.execute(
            """
            UPDATE codegen_llm_attempts
            SET status = 'cancelled', finished_at = NOW(), latency_ms = 0,
                error_classification = 'cancelled'
            WHERE attempt_id = $1
            """,
            executable_attempt_id,
        )
        assert await conn.fetchval(
            "SELECT status FROM codegen_llm_attempts WHERE attempt_id = $1",
            executable_attempt_id,
        ) == "cancelled"
        with pytest.raises(
            asyncpg.CheckViolationError,
            match="Terminal Codegen LLM attempts are immutable",
        ):
            await conn.execute(
                """
                UPDATE codegen_llm_attempts
                SET error_classification = 'unknown'
                WHERE attempt_id = $1
                """,
                attempt_id,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                """
                INSERT INTO codegen_llm_attempts (
                    project_id, changeset_id, phase, role, attempt_sequence,
                    provider, model_id, assignment_version, status,
                    egress_at, finished_at, latency_ms, error_classification
                ) VALUES (
                    $1, $2, 'review', 'helper', 1, 'openai',
                    'gpt-5.4-mini', 1, 'cancelled', NOW(), NOW(), 0,
                    'cancelled'
                )
                """,
                project_id,
                changeset_id,
            )

        unauthorized_project_id = _project_id("noauth")
        unauthorized_changeset_id = f"unauthorized-{uuid.uuid4().hex}"
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = replica")
            await conn.execute(
                "INSERT INTO admin_projects (project_id) VALUES ($1)",
                unauthorized_project_id,
            )
            await conn.execute(
                """
                INSERT INTO codegen_changesets (
                    changeset_id, project_id, task,
                    repository_target_quarantined, idempotency_key,
                    idempotency_request_sha256
                ) VALUES (
                    $1, $2, '{"context":{}}'::JSONB, TRUE, $3, $4
                )
                """,
                unauthorized_changeset_id,
                unauthorized_project_id,
                f"live:{unauthorized_changeset_id}",
                uuid.uuid4().hex + uuid.uuid4().hex,
            )
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError,
            match="requires an operator-provisioned or explicitly authorized project",
        ):
            await conn.execute(
                """
                INSERT INTO codegen_llm_attempts (
                    project_id, changeset_id, phase, role, attempt_sequence,
                    provider, model_id, assignment_version, status
                ) VALUES (
                    $1, $2, 'brief', 'helper', 1, 'openai',
                    'gpt-5.4-mini', 1, 'prepared'
                )
                """,
                unauthorized_project_id,
                unauthorized_changeset_id,
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_prepare_attempt_audits_execution_authority_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert POSTGRES_URL is not None
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "development_pr")
    monkeypatch.setenv("CODEGEN_REVISION", DEVELOPMENT_CODEGEN_REVISION)
    pool = await asyncpg.create_pool(POSTGRES_URL, min_size=1, max_size=2)
    project_id = _project_id("blocked")
    changeset_id = f"blocked-{uuid.uuid4().hex}"
    grant_id = f"ghg_{uuid.uuid4().hex}"
    repository_id = uuid.uuid4().int % 1_000_000_000 + 1
    installation_id = uuid.uuid4().int % 1_000_000_000 + 1
    repository_full_name = f"apdl/{project_id}"
    snapshot = _snapshot(
        project_id,
        grant_id,
        repository_id,
        installation_id,
        repository_full_name,
    )
    snapshot["codegen_revision"] = DEVELOPMENT_CODEGEN_REVISION
    snapshot["behavior_configuration_sha256"] = (
        codegen_tenant_behavior_configuration_sha256()
    )
    snapshot["rollout_stage"] = PublicationStage.development_pr.value
    authorization = build_development_publication_authorization(
        risk=RiskLevel.high,
        model="openai/gpt-5.4-mini",
        codegen_revision=DEVELOPMENT_CODEGEN_REVISION,
    )
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL session_replication_role = replica")
                await conn.execute(
                    "INSERT INTO admin_projects (project_id) VALUES ($1)",
                    project_id,
                )
                await _insert_changeset(
                    conn,
                    changeset_id=changeset_id,
                    project_id=project_id,
                    grant_id=grant_id,
                    repository_id=repository_id,
                    installation_id=installation_id,
                    repository_full_name=repository_full_name,
                    snapshot=snapshot,
                )
            invalid_model = authorization.model_dump(mode="json")
            invalid_model["request"]["model"] = "openai/gpt-4.1"
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    UPDATE codegen_changesets
                    SET publication_authorization = $2::JSONB
                    WHERE changeset_id = $1
                    """,
                    changeset_id,
                    json.dumps(invalid_model),
                )

            numeric_digest = authorization.model_dump(mode="json")
            numeric_digest["authorization_sha256"] = int("1" * 64)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    """
                    UPDATE codegen_changesets
                    SET publication_authorization = $2::JSONB
                    WHERE changeset_id = $1
                    """,
                    changeset_id,
                    json.dumps(numeric_digest),
                )

            await conn.execute(
                """
                UPDATE codegen_changesets
                SET publication_authorization = $2::JSONB
                WHERE changeset_id = $1
                """,
                changeset_id,
                authorization.model_dump_json(),
            )

        with pytest.raises(
            LlmRoutingUnavailableError,
            match="Project execution authority is unavailable",
        ):
            await prepare_llm_attempt(
                pool,
                ProjectCredentialStore(pool, CIPHER),
                changeset_id=changeset_id,
                phase="brief",
            )

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT project_id, changeset_id, phase, role, status,
                       egress_at, credential_id, credential_version,
                       error_classification
                FROM codegen_llm_attempts
                WHERE changeset_id = $1
                """,
                changeset_id,
            )
        assert row is not None
        assert dict(row) == {
            "project_id": project_id,
            "changeset_id": changeset_id,
            "phase": "brief",
            "role": "helper",
            "status": "blocked",
            "egress_at": None,
            "credential_id": None,
            "credential_version": None,
            "error_classification": "execution_authority_unavailable",
        }
    finally:
        await pool.close()
