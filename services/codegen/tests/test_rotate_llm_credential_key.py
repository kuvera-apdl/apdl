"""Secret-free command contract for Codegen credential-key rotation."""

from __future__ import annotations

from uuid import UUID

import pytest

from scripts import rotate_llm_credential_key


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
        actor: str,
    ) -> tuple[int, tuple[UUID, ...]]:
        assert conn is connection
        assert old_cipher is globals_old_cipher
        assert new_cipher is globals_new_cipher
        assert actor == "operator:rotation-test"
        return 1, (audit_id,)

    globals_old_cipher = old_cipher
    globals_new_cipher = new_cipher
    monkeypatch.setenv(
        "CODEGEN_LLM_CREDENTIAL_OLD_ENCRYPTION_KEY_BASE64",
        OLD_SECRET,
    )
    monkeypatch.setenv(
        "CODEGEN_LLM_CREDENTIAL_NEW_ENCRYPTION_KEY_BASE64",
        NEW_SECRET,
    )
    monkeypatch.setenv("POSTGRES_URL", "postgresql://rotation.test/apdl")
    monkeypatch.setattr(
        rotate_llm_credential_key.CredentialCipher,
        "from_base64",
        staticmethod(decode),
    )
    monkeypatch.setattr(rotate_llm_credential_key.asyncpg, "connect", connect)
    monkeypatch.setattr(
        rotate_llm_credential_key,
        "rotate_active_credentials",
        rotate,
    )

    await rotate_llm_credential_key._rotate("operator:rotation-test")

    output = capsys.readouterr().out
    assert decoded == [OLD_SECRET, NEW_SECRET]
    assert OLD_SECRET not in output
    assert NEW_SECRET not in output
    assert output == (
        "Rotated 1 active Codegen credential(s)\n"
        f"Audit IDs: {audit_id}\n"
    )
    assert [args for _, args in executed] == [
        (rotate_llm_credential_key.MAINTENANCE_INHIBITOR_LOCK_ID,),
        (rotate_llm_credential_key.MAINTENANCE_GUARD_LOCK_ID,),
    ]
    assert connection.closed is True
