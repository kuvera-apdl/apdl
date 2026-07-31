#!/usr/bin/env python3
"""Launch one host-run APDL service with a strict secret environment boundary."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Mapping
from pathlib import Path


SERVICES = (
    "admin",
    "admin-api",
    "ingestion",
    "config",
    "query",
    "agents",
    "codegen",
    "pipeline",
)

AGENTS_PLATFORM_KEY = "AGENTS_LLM_CREDENTIAL_ENCRYPTION_KEY_BASE64"
CODEGEN_PLATFORM_KEY = "CODEGEN_LLM_CREDENTIAL_ENCRYPTION_KEY_BASE64"
PLATFORM_KEY_OWNER = {
    AGENTS_PLATFORM_KEY: "agents",
    CODEGEN_PLATFORM_KEY: "codegen",
}

CODEGEN_GITHUB_SECRETS = frozenset(
    {
        "GITHUB_APP_PRIVATE_KEY_BASE64",
        "GITHUB_WEBHOOK_SECRET",
    }
)

# Keep this synchronized with the model-provider credential allow-lists. Provider
# credentials are stored encrypted and released just in time; a long-running
# service never receives an ambient provider credential, including Agents and
# Codegen.
AMBIENT_PROVIDER_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AZURE_API_KEY",
        "COHERE_API_KEY",
        "DEEPSEEK_API_KEY",
        "FIREWORKS_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "TOGETHERAI_API_KEY",
        "XAI_API_KEY",
    }
)

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROTATION_KEY = re.compile(
    r"^(?:AGENTS|CODEGEN)_LLM_CREDENTIAL_"
    r"(?:OLD|NEW)_ENCRYPTION_KEY_BASE64$"
)
_SCOPED_PROVIDER_KEY = re.compile(
    r"^(?:AGENTS|CODEGEN)_[A-Z0-9_]+_API_KEY$"
)


class EnvironmentFileError(ValueError):
    """An environment file is ambiguous or malformed."""


def _quoted_value(raw: str, *, line_number: int) -> str:
    quote = raw[0]
    value: list[str] = []
    index = 1
    while index < len(raw):
        character = raw[index]
        if character == quote:
            remainder = raw[index + 1 :].strip()
            if remainder and not remainder.startswith("#"):
                raise EnvironmentFileError(
                    f"environment file line {line_number} has trailing content"
                )
            return "".join(value)
        if character == "\\" and index + 1 < len(raw):
            escaped = raw[index + 1]
            if quote == '"':
                value.append(
                    {
                        "\\": "\\",
                        '"': '"',
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                    }.get(escaped, f"\\{escaped}")
                )
            elif escaped in {"\\", "'"}:
                value.append(escaped)
            else:
                value.extend(("\\", escaped))
            index += 2
            continue
        value.append(character)
        index += 1
    raise EnvironmentFileError(
        f"environment file line {line_number} has an unterminated quoted value"
    )


def _environment_value(raw: str, *, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        return _quoted_value(value, line_number=line_number)
    inline_comment = re.search(r"\s+#", value)
    if inline_comment is not None:
        value = value[: inline_comment.start()]
    return value.rstrip()


def load_environment_file(path: Path) -> dict[str, str]:
    """Parse a bounded dotenv subset without shell evaluation or interpolation."""
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvironmentFileError("environment file could not be read") from exc
    if "\x00" in contents:
        raise EnvironmentFileError("environment file contains a NUL byte")

    environment: dict[str, str] = {}
    for line_number, line in enumerate(contents.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if "=" not in stripped:
            raise EnvironmentFileError(
                f"environment file line {line_number} is not an assignment"
            )
        name, raw_value = stripped.split("=", 1)
        name = name.strip()
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise EnvironmentFileError(
                f"environment file line {line_number} has an invalid name"
            )
        if name in environment:
            raise EnvironmentFileError(
                f"environment file line {line_number} duplicates a name"
            )
        environment[name] = _environment_value(
            raw_value,
            line_number=line_number,
        )
    return environment


def _permitted(service: str, name: str) -> bool:
    platform_owner = PLATFORM_KEY_OWNER.get(name)
    if platform_owner is not None:
        return service == platform_owner
    if name in CODEGEN_GITHUB_SECRETS:
        return service == "codegen"
    if name in AMBIENT_PROVIDER_KEYS:
        return False
    if _SCOPED_PROVIDER_KEY.fullmatch(name) is not None:
        return False
    if _ROTATION_KEY.fullmatch(name) is not None:
        return False
    return True


def service_environment(
    service: str,
    *,
    inherited: Mapping[str, str],
    file_values: Mapping[str, str],
) -> dict[str, str]:
    """Merge dotenv + inherited values, then enforce the serving secret boundary."""
    if service not in SERVICES:
        raise ValueError("unknown host-run service")
    combined = dict(file_values)
    # Match normal dotenv behavior: an explicit inherited value wins over the file.
    combined.update(inherited)
    return {
        name: value
        for name, value in combined.items()
        if _permitted(service, name)
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", choices=SERVICES, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--working-directory", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if arguments.command and arguments.command[0] == "--":
        arguments.command.pop(0)
    if not arguments.command:
        parser.error("a command is required after --")
    return arguments


def main() -> None:
    arguments = _arguments()
    try:
        file_values = (
            load_environment_file(arguments.env_file)
            if arguments.env_file is not None
            else {}
        )
        environment = service_environment(
            arguments.service,
            inherited=os.environ,
            file_values=file_values,
        )
        working_directory = arguments.working_directory.resolve(strict=True)
        if not working_directory.is_dir():
            raise ValueError("working directory is not a directory")
        os.chdir(working_directory)
        os.execvpe(arguments.command[0], arguments.command, environment)
    except (EnvironmentFileError, OSError, ValueError) as exc:
        # Errors name only the failed contract, never environment values.
        raise SystemExit(f"host service launch failed: {exc}") from None


if __name__ == "__main__":
    main()
