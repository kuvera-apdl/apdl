"""Secret-free contract for the vault-wide key-rotation command."""

from __future__ import annotations

from uuid import UUID

import pytest

from app import rotate_key_cli


OLD_SECRET = "old-encryption-key-material-sentinel"
NEW_SECRET = "new-encryption-key-material-sentinel"


@pytest.mark.asyncio
async def test_rotation_cli_acquires_both_locks_and_prints_no_key_material(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_cipher = object()
    new_cipher = object()
    decoded: list[str] = []
    executed: list[tuple[str, tuple[object, ...]]] = []
    audit_id = UUID("50000000-0000-4000-8000-000000000005")

    def decode(value: str) -> object:
        decoded.append(value)
        return old_cipher if value == OLD_SECRET else new_cipher

    class Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

    class Connection:
        closed = False

        async def execute(self, query: str, *args: object) -> None:
            executed.append((query, args))

        def transaction(self) -> Transaction:
            return Transaction()

        async def close(self) -> None:
            self.closed = True

    connection = Connection()

    async def connect(_url: str) -> Connection:
        return connection

    async def rotate(
        conn: Connection,
        *,
        old_cipher: object,
        new_cipher: object,
        operator: str,
    ) -> tuple[int, tuple[UUID, ...]]:
        assert conn is connection
        assert old_cipher is globals_old_cipher
        assert new_cipher is globals_new_cipher
        assert operator == "operator:rotation-test"
        return 1, (audit_id,)

    globals_old_cipher = old_cipher
    globals_new_cipher = new_cipher
    monkeypatch.setenv("LLM_VAULT_OLD_ENCRYPTION_KEY_BASE64", OLD_SECRET)
    monkeypatch.setenv("LLM_VAULT_NEW_ENCRYPTION_KEY_BASE64", NEW_SECRET)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://rotation.test/apdl")
    monkeypatch.setattr(
        rotate_key_cli.CredentialCipher,
        "from_base64",
        staticmethod(decode),
    )
    monkeypatch.setattr(rotate_key_cli.asyncpg, "connect", connect)
    monkeypatch.setattr(
        rotate_key_cli,
        "rotate_active_credentials",
        rotate,
    )

    await rotate_key_cli._rotate("operator:rotation-test")

    output = capsys.readouterr().out
    assert decoded == [OLD_SECRET, NEW_SECRET]
    assert OLD_SECRET not in output
    assert NEW_SECRET not in output
    assert output == (
        "Rotated 1 active vault credential(s)\n"
        f"Audit IDs: {audit_id}\n"
    )
    assert [args for _, args in executed] == [
        (rotate_key_cli.MAINTENANCE_INHIBITOR_LOCK_ID,),
        (rotate_key_cli.MAINTENANCE_GUARD_LOCK_ID,),
    ]
    assert connection.closed is True
