#!/usr/bin/env python3
"""Offline, all-or-nothing rotation of the Codegen LLM credential key."""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg

from app.config import postgres_url
from app.store.llm_credentials import CredentialCipher, rotate_active_credentials


MAINTENANCE_INHIBITOR_LOCK_ID = 4_158_044_083
MAINTENANCE_GUARD_LOCK_ID = 4_158_044_084


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-encrypt active Codegen project LLM credentials. Drain all APDL "
            "PostgreSQL runtimes first; this command waits for the exclusive "
            "maintenance barrier."
        )
    )
    parser.add_argument("--actor", required=True)
    return parser.parse_args()


async def _rotate(actor: str) -> None:
    old_cipher = CredentialCipher.from_base64(
        os.getenv("CODEGEN_LLM_CREDENTIAL_OLD_ENCRYPTION_KEY_BASE64", "")
    )
    new_cipher = CredentialCipher.from_base64(
        os.getenv("CODEGEN_LLM_CREDENTIAL_NEW_ENCRYPTION_KEY_BASE64", "")
    )
    conn = await asyncpg.connect(postgres_url())
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
                actor=actor,
            )
        print(f"Rotated {count} active Codegen credential(s)")
        print("Audit IDs: " + ",".join(str(item) for item in audit_ids))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_rotate(_arguments().actor))
