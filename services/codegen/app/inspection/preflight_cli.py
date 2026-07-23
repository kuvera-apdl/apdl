"""Entrypoint for provider-free repository preparation and contract evidence."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.editor.worker_contract import (
    read_codegen_preparation_request,
)
from app.egress import EGRESS_PROXY_ENV
from app.inspection.preparation import (
    RepositoryPreparationFailure,
    RepositoryPreparationSuccess,
    prepare_repository,
)


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
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": os.devnull,
        "GIT_CONFIG_KEY_1": "http.extraHeader",
        "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {encoded}",
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
        # Drop the clone subprocess environment and local encoded copies before
        # any repository path is inspected.
        token = ""
        encoded = ""
        environment.clear()
    if completed.returncode != 0:
        raise RuntimeError("repository preflight clone failed")


def main() -> int:
    try:
        envelope = read_codegen_preparation_request(sys.stdin.buffer)
        request_sha256 = envelope.request_sha256()
        request = envelope.to_edit_request()
        del envelope
        source_branch = (
            request.branch if request.existing_branch else request.base_branch
        )
        token = request.token
        workdir = os.environ.get("CODEGEN_WORKDIR", "/workspace")
        with tempfile.TemporaryDirectory(prefix="apdl-inspect-", dir=workdir) as tmp:
            repo_dir = Path(tmp) / "repo"
            _clone(
                repository=request.repo,
                source_branch=source_branch,
                token=token,
                destination=repo_dir,
            )
            request.token = ""
            token = ""
            preparation = prepare_repository(
                repo_dir,
                request,
                request_sha256=request_sha256,
                workdir_base=Path(tmp),
            )
    except Exception as exc:
        # Repository text and subprocess stderr never cross this boundary.
        print(
            RepositoryPreparationFailure(
                error=f"repository preflight refused: {type(exc).__name__}"
            ).model_dump_json()
        )
        return 1
    print(
        RepositoryPreparationSuccess(
            preparation=preparation,
        ).model_dump_json()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
