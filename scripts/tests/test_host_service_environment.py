"""Runtime and static contracts for host-run service secret isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_host_service.py"
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")

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
AGENTS_KEY = "AGENTS_LLM_CREDENTIAL_ENCRYPTION_KEY_BASE64"
CODEGEN_KEY = "CODEGEN_LLM_CREDENTIAL_ENCRYPTION_KEY_BASE64"
GITHUB_PRIVATE_KEY = "GITHUB_APP_PRIVATE_KEY_BASE64"
GITHUB_WEBHOOK_SECRET = "GITHUB_WEBHOOK_SECRET"

FILE_SECRETS = {
    AGENTS_KEY: "file-agents-platform-secret-sentinel",
    "CODEGEN_LLM_CREDENTIAL_OLD_ENCRYPTION_KEY_BASE64": (
        "file-codegen-old-platform-secret-sentinel"
    ),
    "OPENAI_API_KEY": "file-openai-provider-secret-sentinel",
    "GEMINI_API_KEY": "file-gemini-provider-secret-sentinel",
    "CODEGEN_EVALUATION_OPENAI_API_KEY": (
        "file-retired-scoped-provider-secret-sentinel"
    ),
    "AGENTS_RETIRED_GEMINI_API_KEY": (
        "file-other-scoped-provider-secret-sentinel"
    ),
    GITHUB_PRIVATE_KEY: "file-github-private-secret-sentinel",
}
INHERITED_SECRETS = {
    CODEGEN_KEY: "inherited-codegen-platform-secret-sentinel",
    "AGENTS_LLM_CREDENTIAL_NEW_ENCRYPTION_KEY_BASE64": (
        "inherited-agents-new-platform-secret-sentinel"
    ),
    "ANTHROPIC_API_KEY": "inherited-anthropic-provider-secret-sentinel",
    "XAI_API_KEY": "inherited-xai-provider-secret-sentinel",
    GITHUB_WEBHOOK_SECRET: "inherited-github-webhook-secret-sentinel",
}
ALL_SECRET_VALUES = tuple((*FILE_SECRETS.values(), *INHERITED_SECRETS.values()))

CHILD = """
import json
import os
import sys

interesting = sorted(
    name
    for name in os.environ
    if (
        name.endswith("_API_KEY")
        or "LLM_CREDENTIAL" in name
        or name in {"GITHUB_APP_PRIVATE_KEY_BASE64", "GITHUB_WEBHOOK_SECRET"}
    )
)
secret_values = [os.environ[name] for name in interesting if os.environ[name]]
print(json.dumps({
    "names": interesting,
    "secret_value_in_argv": any(
        value in argument
        for value in secret_values
        for argument in sys.argv
    ),
}, sort_keys=True))
"""


def _target_recipe(target: str) -> str:
    marker = f"{target}:\n"
    start = MAKEFILE.index(marker) + len(marker)
    lines: list[str] = []
    for line in MAKEFILE[start:].splitlines():
        if not line.startswith("\t"):
            break
        lines.append(line)
    return "\n".join(lines)


class HostServiceEnvironmentTests(unittest.TestCase):
    def test_every_host_run_target_uses_the_strict_wrapper(self) -> None:
        self.assertIn(
            "HOST_SERVICE_RUNNER := python3 scripts/run_host_service.py",
            MAKEFILE,
        )
        self.assertNotIn("\nSERVICE_ENV_FILE :=", MAKEFILE)
        self.assertNotIn("$(SERVICE_ENV_FILE)", MAKEFILE)
        for service in SERVICES:
            recipe = _target_recipe(f"run-{service}")
            self.assertIn("$(HOST_SERVICE_RUNNER)", recipe, service)
            self.assertIn(f"--service {service}", recipe, service)
            self.assertNotIn("uv run --no-project", recipe, service)

    def test_file_and_inherited_secrets_are_filtered_by_service_owner(self) -> None:
        expected = {
            service: set()
            for service in SERVICES
        }
        expected["agents"] = {AGENTS_KEY}
        expected["codegen"] = {
            CODEGEN_KEY,
            GITHUB_PRIVATE_KEY,
            GITHUB_WEBHOOK_SECRET,
        }

        with tempfile.TemporaryDirectory() as temporary:
            boundary = Path(temporary)
            env_file = boundary / "service.env"
            scratch = boundary / "scratch"
            scratch.mkdir()
            env_file.write_text(
                "".join(f"{name}={value}\n" for name, value in FILE_SECRETS.items())
                + "SAFE_FILE_SETTING=loaded-without-shell-evaluation\n",
                encoding="utf-8",
            )
            inherited = {
                "HOME": os.environ.get("HOME", str(boundary)),
                "PATH": os.environ.get("PATH", os.defpath),
                "TMPDIR": str(scratch),
                **INHERITED_SECRETS,
            }

            for service in SERVICES:
                with self.subTest(service=service):
                    command = [
                        sys.executable,
                        str(RUNNER),
                        "--service",
                        service,
                        "--env-file",
                        str(env_file),
                        "--working-directory",
                        str(ROOT),
                        "--",
                        sys.executable,
                        "-c",
                        CHILD,
                    ]
                    rendered_command = "\0".join(command)
                    for value in ALL_SECRET_VALUES:
                        self.assertNotIn(value, rendered_command)
                    completed = subprocess.run(
                        command,
                        env=inherited,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    result = json.loads(completed.stdout)
                    self.assertEqual(set(result["names"]), expected[service])
                    self.assertFalse(result["secret_value_in_argv"])
                    for value in ALL_SECRET_VALUES:
                        self.assertNotIn(value, completed.stdout)
                        self.assertNotIn(value, completed.stderr)

            self.assertEqual(list(scratch.iterdir()), [])

    def test_malformed_environment_file_fails_without_rendering_its_value(self) -> None:
        secret = "malformed-environment-secret-sentinel"
        with tempfile.TemporaryDirectory() as temporary:
            boundary = Path(temporary)
            env_file = boundary / "service.env"
            env_file.write_text(
                f"SAFE=value\nSAFE={secret}\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--service",
                    "query",
                    "--env-file",
                    str(env_file),
                    "--working-directory",
                    str(ROOT),
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(99)",
                ],
                env={
                    "HOME": os.environ.get("HOME", str(boundary)),
                    "PATH": os.environ.get("PATH", os.defpath),
                },
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("duplicates a name", completed.stderr)
        self.assertNotIn(secret, completed.stderr)


if __name__ == "__main__":
    unittest.main()
