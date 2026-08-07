#!/usr/bin/env python3
"""Generate and persist required local LLM vault secrets."""

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
ADMIN_TOKEN_NAME = "LLM_VAULT_ADMIN_TOKEN"
ADMIN_TOKEN_BYTES = 32


class SecretConfigurationError(ValueError):
    """A canonical local vault-secret assignment is ambiguous or malformed."""


def generate_key() -> str:
    """Return canonical Base64 for a newly generated AES-256 key."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def generate_admin_token() -> str:
    """Return a normalized token with at least 32 random bytes of entropy."""
    return secrets.token_urlsafe(ADMIN_TOKEN_BYTES)


def _validate_key(value: str) -> None:
    if not value or value != value.strip() or not value.isascii():
        raise SecretConfigurationError(
            f"{KEY_NAME} must be canonical Base64 for exactly {KEY_BYTES} random bytes"
        )
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretConfigurationError(
            f"{KEY_NAME} must be canonical Base64 for exactly {KEY_BYTES} random bytes"
        ) from exc
    if len(decoded) != KEY_BYTES or base64.b64encode(decoded).decode("ascii") != value:
        raise SecretConfigurationError(
            f"{KEY_NAME} must be canonical Base64 for exactly {KEY_BYTES} random bytes"
        )


def _validate_admin_token(value: str) -> None:
    if value != value.strip() or len(value.encode("utf-8")) < 32:
        raise SecretConfigurationError(
            f"{ADMIN_TOKEN_NAME} must contain at least 32 normalized bytes"
        )


def _ensure_assignments(
    env_file: Path,
    *,
    specifications: tuple[
        tuple[str, Callable[[str], None], Callable[[], str]], ...
    ],
) -> frozenset[str]:
    """Fill missing assignments atomically without rotating existing values."""
    source = env_file.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    locations: dict[str, int | None] = {}
    generated: set[str] = set()

    for name, validator, _factory in specifications:
        prefix = f"{name}="
        assignments = [
            index for index, line in enumerate(lines) if line.startswith(prefix)
        ]
        if len(assignments) > 1:
            raise SecretConfigurationError(f"{name} must be assigned exactly once")
        index = assignments[0] if assignments else None
        locations[name] = index
        if index is not None:
            existing = lines[index].removesuffix("\n").removesuffix("\r")[
                len(prefix) :
            ]
            if existing:
                validator(existing)

    for name, validator, factory in specifications:
        index = locations[name]
        if index is not None:
            existing = lines[index].removesuffix("\n").removesuffix("\r").split(
                "=", 1
            )[1]
            if existing:
                continue
        value = factory()
        validator(value)
        assignment = f"{name}={value}\n"
        if index is None:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] = f"{lines[-1]}\n"
            lines.append(assignment)
        else:
            lines[index] = assignment
        generated.add(name)

    if not generated:
        return frozenset()

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
    return frozenset(generated)


def ensure_key(
    env_file: Path,
    *,
    key_factory: Callable[[], str] = generate_key,
) -> bool:
    """Fill one missing or empty canonical key without rotating it."""
    generated = _ensure_assignments(
        env_file,
        specifications=((KEY_NAME, _validate_key, key_factory),),
    )
    return KEY_NAME in generated


def ensure_local_secrets(
    env_file: Path,
    *,
    key_factory: Callable[[], str] = generate_key,
    admin_token_factory: Callable[[], str] = generate_admin_token,
) -> frozenset[str]:
    """Provision the required key and admin token in one atomic rewrite."""
    return _ensure_assignments(
        env_file,
        specifications=(
            (KEY_NAME, _validate_key, key_factory),
            (ADMIN_TOKEN_NAME, _validate_admin_token, admin_token_factory),
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="print a fresh canonical key")
    ensure = commands.add_parser(
        "ensure", help="persist required secrets in an environment file"
    )
    ensure.add_argument("env_file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "generate":
            print(generate_key())
            return 0
        generated = ensure_local_secrets(arguments.env_file)
    except (SecretConfigurationError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    state = ", ".join(sorted(generated)) if generated else "existing secrets"
    print(f"provisioned {state} in {arguments.env_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
