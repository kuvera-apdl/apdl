#!/usr/bin/env python3
"""Generate and persist the local LLM vault encryption key."""

from __future__ import annotations

import argparse
import base64
import binascii
import os
import secrets
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path


KEY_NAME = "LLM_VAULT_ENCRYPTION_KEY_BASE64"
KEY_BYTES = 32


class KeyConfigurationError(ValueError):
    """The canonical local vault-key assignment is ambiguous or malformed."""


def generate_key() -> str:
    """Return canonical Base64 for a newly generated AES-256 key."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def _validate_key(value: str) -> None:
    if not value or value != value.strip() or not value.isascii():
        raise KeyConfigurationError(
            f"{KEY_NAME} must be canonical Base64 for exactly {KEY_BYTES} random bytes"
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyConfigurationError(
            f"{KEY_NAME} must be canonical Base64 for exactly {KEY_BYTES} random bytes"
        ) from exc
    if len(decoded) != KEY_BYTES or base64.b64encode(decoded).decode("ascii") != value:
        raise KeyConfigurationError(
            f"{KEY_NAME} must be canonical Base64 for exactly {KEY_BYTES} random bytes"
        )


def ensure_key(
    env_file: Path,
    *,
    key_factory: Callable[[], str] = generate_key,
) -> bool:
    """Fill one missing or empty canonical assignment without rotating it."""
    source = env_file.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    prefix = f"{KEY_NAME}="
    assignments = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(assignments) > 1:
        raise KeyConfigurationError(f"{KEY_NAME} must be assigned exactly once")

    if assignments:
        index = assignments[0]
        existing = lines[index].removesuffix("\n").removesuffix("\r")[len(prefix) :]
        if existing:
            _validate_key(existing)
            return False
    else:
        index = len(lines)
        if source and not source.endswith(("\n", "\r")):
            lines.append("\n")

    value = key_factory()
    _validate_key(value)
    assignment = f"{prefix}{value}\n"
    if assignments:
        lines[index] = assignment
    else:
        lines.append(assignment)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=env_file.parent,
            prefix=f".{env_file.name}.",
            delete=False,
        ) as temporary:
            temporary.write("".join(lines))
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, env_file)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="print a fresh canonical key")
    ensure = commands.add_parser("ensure", help="persist a key in an environment file")
    ensure.add_argument("env_file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            print(generate_key())
            return 0
        generated = ensure_key(arguments.env_file)
    except (KeyConfigurationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    state = "generated" if generated else "preserved"
    print(f"{state} {KEY_NAME} in {arguments.env_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
