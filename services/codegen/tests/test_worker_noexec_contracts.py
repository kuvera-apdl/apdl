"""Built-worker regression for contract checks on production noexec mounts."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest


def _docker_daemon_available(docker: str) -> bool:
    try:
        completed = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _assert_completed(completed: subprocess.CompletedProcess[str], label: str) -> None:
    if completed.returncode == 0:
        return
    output = (completed.stdout + "\n" + completed.stderr)[-8000:]
    pytest.fail(f"{label} failed with exit {completed.returncode}:\n{output}")


def test_built_worker_contract_checks_survive_production_noexec_mounts():
    docker = shutil.which("docker")
    if docker is None or not _docker_daemon_available(docker):
        pytest.skip("Docker daemon is unavailable")

    repository_root = Path(__file__).resolve().parents[3]
    context = repository_root / "services/codegen"
    dockerfile = context / "Dockerfile.worker"
    tag = f"apdl-codegen-noexec-test:{uuid.uuid4().hex}"
    revision = f"noexec-contract-test-{uuid.uuid4().hex}"
    build = subprocess.run(
        [
            docker,
            "build",
            "--build-arg",
            f"CODEGEN_REVISION={revision}",
            "--file",
            dockerfile.as_posix(),
            "--tag",
            tag,
            context.as_posix(),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    _assert_completed(build, "worker image build")

    probe = textwrap.dedent(
        """
        import errno
        import json
        import subprocess
        from pathlib import Path

        from app.contracts.installer import SandboxedCheckRunner
        from app.contracts.models import (
            ContractCheckRequest,
            ContractCheckStatus,
        )

        workspace = Path("/workspace")
        work = workspace / "contract-work"
        work.mkdir(parents=True)

        def write(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(0o755)
            return path

        def assert_noexec(path):
            try:
                subprocess.run(
                    [path.as_posix()],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            except OSError as exc:
                assert exc.errno in {errno.EACCES, errno.EPERM}, repr(exc)
                return
            raise AssertionError(f"{path} executed directly from a noexec mount")

        node_root = workspace / "node-contract"
        tsc = write(
            node_root / "node_modules/typescript/bin/tsc",
            "#!/usr/bin/env node\\nprocess.exit(0);\\n",
        )
        write(
            node_root / "node_modules/typescript/package.json",
            '{"version":"5.7.2"}',
        )
        assert_noexec(tsc)
        node_result = SandboxedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="node",
                package_name="example-sdk",
                exact_version="1.2.3",
                installed_root=node_root.as_posix(),
                language="TypeScript",
                snippet='const value: string = "ok";',
            )
        )
        assert node_result.status is ContractCheckStatus.passed, node_result
        node_command = node_result.command.split()
        assert Path(node_command[0]).is_absolute()
        assert not node_command[0].startswith("/workspace/")
        assert node_command[1] == tsc.as_posix()

        python_root = workspace / "python-contract"
        mypy = write(
            python_root / "bin/mypy",
            "#!/usr/bin/env python3\\nraise SystemExit(0)\\n",
        )
        write(
            python_root
            / "lib/python3.12/site-packages/mypy-1.14.1.dist-info/METADATA",
            "Name: mypy\\nVersion: 1.14.1\\n",
        )
        assert_noexec(mypy)
        python_result = SandboxedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="python",
                package_name="example-sdk",
                exact_version="1.2.3",
                installed_root=python_root.as_posix(),
                language="Python",
                snippet='value: str = "ok"',
            )
        )
        assert python_result.status is ContractCheckStatus.passed, python_result
        python_command = python_result.command.split()
        assert Path(python_command[0]).is_absolute()
        assert not python_command[0].startswith("/workspace/")
        assert python_command[1] == mypy.as_posix()

        print(
            json.dumps(
                {
                    "node_status": node_result.status.value,
                    "python_status": python_result.status.value,
                    "node_command": node_result.command,
                    "python_command": python_result.command,
                },
                sort_keys=True,
            )
        )
        """
    )
    try:
        run = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "512",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--tmpfs",
                "/workspace:rw,nosuid,nodev,noexec,size=4g,uid=1000,gid=1000",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=512m,uid=1000,gid=1000",
                "--user",
                "1000:1000",
                "-e",
                "HOME=/workspace/home",
                "-e",
                "TMPDIR=/workspace/tmp",
                "--entrypoint",
                "python",
                tag,
                "-c",
                probe,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        _assert_completed(run, "production-mount contract probe")
        result = json.loads(run.stdout.strip().splitlines()[-1])
        assert result["node_status"] == "passed"
        assert result["python_status"] == "passed"
    finally:
        subprocess.run(
            [docker, "image", "rm", "--force", tag],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
