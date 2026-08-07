"""Contracts for per-install and per-smoke LLM vault encryption keys."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from scripts import llm_vault_key


ROOT = Path(__file__).resolve().parents[2]
PUBLISHED_ZERO_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class LlmVaultKeyTests(unittest.TestCase):
    def test_generate_returns_fresh_canonical_32_byte_keys(self) -> None:
        first = llm_vault_key.generate_key()
        second = llm_vault_key.generate_key()

        self.assertNotEqual(first, second)
        for value in (first, second):
            decoded = base64.b64decode(value, validate=True)
            self.assertEqual(len(decoded), 32)
            self.assertEqual(base64.b64encode(decoded).decode("ascii"), value)

    def test_ensure_fills_empty_assignment_once_and_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "LOG_LEVEL=info\nLLM_VAULT_ENCRYPTION_KEY_BASE64=\n",
                encoding="utf-8",
            )

            self.assertTrue(llm_vault_key.ensure_key(env_file))
            generated = env_file.read_text(encoding="utf-8")
            self.assertFalse(llm_vault_key.ensure_key(env_file))

            self.assertEqual(env_file.read_text(encoding="utf-8"), generated)
            assignment = generated.split("LLM_VAULT_ENCRYPTION_KEY_BASE64=", 1)[1]
            value = assignment.splitlines()[0]
            self.assertEqual(len(base64.b64decode(value, validate=True)), 32)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_ensure_rejects_duplicate_canonical_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "LLM_VAULT_ENCRYPTION_KEY_BASE64=\n"
                "LLM_VAULT_ENCRYPTION_KEY_BASE64=\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                llm_vault_key.KeyConfigurationError,
                "must be assigned exactly once",
            ):
                llm_vault_key.ensure_key(env_file)

    def test_setup_smoke_and_compose_have_no_working_default(self) -> None:
        dev = (ROOT / "scripts" / "dev.sh").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts" / "smoke_fresh_install.sh").read_text(
            encoding="utf-8"
        )
        compose = (ROOT / "infra" / "docker" / "docker-compose.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'python3 "$ROOT_DIR/scripts/llm_vault_key.py" ensure "$ROOT_DIR/.env"',
            dev,
        )
        self.assertIn(
            'python3 "$ROOT_DIR/scripts/llm_vault_key.py" generate',
            smoke,
        )
        self.assertIn(
            "LLM_VAULT_ENCRYPTION_KEY_BASE64: "
            "${LLM_VAULT_ENCRYPTION_KEY_BASE64:-}",
            compose,
        )
        self.assertNotIn(PUBLISHED_ZERO_KEY, smoke)
        self.assertNotIn(PUBLISHED_ZERO_KEY, compose)


if __name__ == "__main__":
    unittest.main()
