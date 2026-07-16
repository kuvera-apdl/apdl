"""Entrypoint for the credential-free repository-inspection container."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.egress import EGRESS_PROXY_ENV
from app.inspection.preflight import attest_repository_checkout


def _read_token() -> str:
    """Consume the read token from stdin so it never enters process environ."""
    try:
        payload = json.loads(sys.stdin.readline())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("missing repository read credential") from exc
    if not isinstance(payload, dict) or set(payload) != {"read_token"}:
        raise ValueError("invalid repository read credential envelope")
    token = payload["read_token"]
    if not isinstance(token, str) or not token:
        raise ValueError("missing repository read credential")
    return token


def _clone(
    *,
    repository: str,
    source_branch: str,
    token: str,
    destination: Path,
) -> None:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
    }
    for name in EGRESS_PROXY_ENV:
        if name in os.environ:
            environment[name] = os.environ[name]
    try:
        completed = subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                source_branch,
                f"https://github.com/{repository}.git",
                str(destination),
            ],
            check=False,
            capture_output=True,
            timeout=120,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("repository preflight clone failed") from exc
    finally:
        # Drop the only in-process copy before any repository path is inspected.
        token = ""
        encoded = ""
        environment.clear()
    if completed.returncode != 0:
        raise RuntimeError("repository preflight clone failed")


def main() -> int:
    try:
        repository = os.environ["CS_REPO"]
        source_branch = os.environ["CS_SOURCE_BRANCH"]
        token = _read_token()
        workdir = os.environ.get("CODEGEN_WORKDIR", "/workspace")
        with tempfile.TemporaryDirectory(prefix="apdl-inspect-", dir=workdir) as tmp:
            repo_dir = Path(tmp) / "repo"
            _clone(
                repository=repository,
                source_branch=source_branch,
                token=token,
                destination=repo_dir,
            )
            token = ""
            attestation = attest_repository_checkout(
                repo_dir,
                repository=repository,
                source_branch=source_branch,
            )
    except Exception as exc:
        # Repository text and subprocess stderr never cross this boundary.
        print(
            json.dumps(
                {
                    "success": False,
                    "error": f"repository preflight refused: {type(exc).__name__}",
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "success": True,
                "attestation": attestation.model_dump(mode="json"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
