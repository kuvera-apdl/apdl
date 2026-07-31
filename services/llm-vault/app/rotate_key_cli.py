"""Rotate every active vault credential under the maintenance barrier."""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg

from app.crypto import CredentialCipher
from app.rotation import (
    MAINTENANCE_GUARD_LOCK_ID,
    MAINTENANCE_INHIBITOR_LOCK_ID,
    rotate_active_credentials,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-encrypt every active project LLM credential. Drain all APDL "
            "PostgreSQL runtimes first; this command waits for both exclusive "
            "maintenance barriers."
        )
    )
    parser.add_argument("--operator", required=True)
    return parser.parse_args()


async def _rotate(operator: str) -> None:
    old_cipher = CredentialCipher.from_base64(
        os.getenv("LLM_VAULT_OLD_ENCRYPTION_KEY_BASE64", "")
    )
    new_cipher = CredentialCipher.from_base64(
        os.getenv("LLM_VAULT_NEW_ENCRYPTION_KEY_BASE64", "")
    )
    dsn = os.getenv(
        "POSTGRES_URL",
        "postgresql://apdl_llm_vault:apdl_llm_vault_dev@localhost:5432/apdl",
    )
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "SELECT pg_advisory_lock($1)", MAINTENANCE_INHIBITOR_LOCK_ID
        )
        await conn.execute(
            "SELECT pg_advisory_lock($1)", MAINTENANCE_GUARD_LOCK_ID
        )
        async with conn.transaction():
            count, audit_ids = await rotate_active_credentials(
                conn,
                old_cipher=old_cipher,
                new_cipher=new_cipher,
                operator=operator,
            )
        print(f"Rotated {count} active vault credential(s)")
        print("Audit IDs: " + ",".join(str(item) for item in audit_ids))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_rotate(_arguments().operator))
