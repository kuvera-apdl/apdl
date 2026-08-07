"""Live PostgreSQL checks for immutable, complete LLM setup bindings.

Set ``APDL_AGENTS_SETUP_TEST_POSTGRES_URL`` to a disposable, fully migrated
database owned by a superuser. The normal unit suite does not require a live
database; CI runs this file explicitly after applying the canonical sequence.
"""

from __future__ import annotations

import os

import asyncpg
import pytest


POSTGRES_URL = (
    os.getenv("APDL_AGENTS_SETUP_TEST_POSTGRES_URL", "").strip() or None
)

if os.getenv("GITHUB_ACTIONS") == "true" and POSTGRES_URL is None:
    raise RuntimeError(
        "APDL_AGENTS_SETUP_TEST_POSTGRES_URL is required in GitHub Actions; "
        "the setup-binding suite must not be skipped"
    )

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="APDL_AGENTS_SETUP_TEST_POSTGRES_URL is not configured",
)


async def _install_binding_trigger(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TRIGGER llm_setup_binding_probe_immutable
        BEFORE INSERT OR UPDATE OF
            setup_version, model_tier, connection_version, inventory_version,
            model_catalog_version, legacy_unbound_setup
        ON llm_setup_binding_probe
        FOR EACH ROW
        EXECUTE FUNCTION apdl_protect_llm_attempt_setup_binding()
        """
    )


@pytest.mark.asyncio
async def test_setup_binding_constraint_and_trigger_reject_null_transition() -> None:
    assert POSTGRES_URL is not None
    conn = await asyncpg.connect(POSTGRES_URL)
    try:
        applied_name = await conn.fetchval(
            "SELECT name FROM apdl_schema_migrations WHERE version = 51"
        )
        assert applied_name == "051_agents_project_setup.sql"
        constraint_definition = await conn.fetchval(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'llm_provider_attempts'::regclass
              AND conname = 'llm_provider_attempts_setup_binding_check'
            """
        )
        assert constraint_definition is not None

        await conn.execute(
            """
            CREATE TEMP TABLE llm_setup_binding_probe (
                probe_id INTEGER PRIMARY KEY,
                setup_version BIGINT,
                model_tier TEXT,
                connection_version BIGINT,
                inventory_version BIGINT,
                model_catalog_version TEXT,
                legacy_unbound_setup BOOLEAN NOT NULL
            )
            """
        )
        # The definition comes from PostgreSQL's own catalog for the migrated
        # production constraint, not from test or user input.
        await conn.execute(
            "ALTER TABLE llm_setup_binding_probe "
            "ADD CONSTRAINT llm_provider_attempts_setup_binding_check "
            f"{constraint_definition}"
        )
        await conn.execute(
            """
            INSERT INTO llm_setup_binding_probe (
                probe_id, setup_version, model_tier, connection_version,
                inventory_version, model_catalog_version,
                legacy_unbound_setup
            ) VALUES (1, NULL, NULL, NULL, NULL, NULL, TRUE)
            """
        )

        await _install_binding_trigger(conn)
        with pytest.raises(
            asyncpg.CheckViolationError,
            match="LLM attempt setup binding is immutable",
        ):
            await conn.execute(
                """
                UPDATE llm_setup_binding_probe
                SET legacy_unbound_setup = FALSE
                WHERE probe_id = 1
                """
            )

        await conn.execute(
            "DROP TRIGGER llm_setup_binding_probe_immutable "
            "ON llm_setup_binding_probe"
        )
        with pytest.raises(
            asyncpg.CheckViolationError,
            match="llm_provider_attempts_setup_binding_check",
        ):
            await conn.execute(
                """
                UPDATE llm_setup_binding_probe
                SET legacy_unbound_setup = FALSE
                WHERE probe_id = 1
                """
            )

        bound_values: list[object] = [
            7,
            "fast",
            3,
            2,
            "llm-provider-catalog@2",
            False,
        ]
        await conn.execute(
            """
            INSERT INTO llm_setup_binding_probe (
                probe_id, setup_version, model_tier, connection_version,
                inventory_version, model_catalog_version,
                legacy_unbound_setup
            ) VALUES (2, $1, $2, $3, $4, $5, $6)
            """,
            *bound_values,
        )

        await _install_binding_trigger(conn)
        with pytest.raises(
            asyncpg.CheckViolationError,
            match="LLM attempt setup binding is immutable",
        ):
            await conn.execute(
                """
                UPDATE llm_setup_binding_probe
                SET legacy_unbound_setup = TRUE
                WHERE probe_id = 2
                """
            )

        for offset in range(5):
            partial_values = bound_values.copy()
            partial_values[offset] = None
            with pytest.raises(
                asyncpg.CheckViolationError,
                match="llm_provider_attempts_setup_binding_check",
            ):
                await conn.execute(
                    """
                    INSERT INTO llm_setup_binding_probe (
                        probe_id, setup_version, model_tier,
                        connection_version, inventory_version,
                        model_catalog_version, legacy_unbound_setup
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    10 + offset,
                    *partial_values,
                )
    finally:
        await conn.close()
