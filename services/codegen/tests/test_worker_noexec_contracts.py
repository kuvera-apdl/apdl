"""Daemon-backed smoke contracts for the production Codegen worker image."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from app.editor.base import EditRequest
from app.editor.container_editor import ContainerAiderEditor


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


@pytest.fixture(scope="module")
def built_worker_image():
    docker = shutil.which("docker")
    if docker is None or not _docker_daemon_available(docker):
        pytest.skip("Docker daemon is unavailable")

    repository_root = Path(__file__).resolve().parents[3]
    context = repository_root / "services/codegen"
    dockerfile = context / "Dockerfile.worker"
    tag = f"apdl-codegen-worker-smoke:{uuid.uuid4().hex}"
    revision = f"worker-smoke-{uuid.uuid4().hex}"
    try:
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
        inspect = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", tag],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        _assert_completed(inspect, "worker image identity inspection")
        image_id = inspect.stdout.strip()
        assert image_id.startswith("sha256:")
        yield docker, image_id
    finally:
        subprocess.run(
            [docker, "image", "rm", "--force", tag],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_built_worker_contract_checks_survive_production_noexec_mounts(
    built_worker_image,
):
    docker, image_id = built_worker_image

    probe = textwrap.dedent(
        """
        import errno
        import json
        import subprocess
        from pathlib import Path

        from app.contracts.installer import ImageOwnedCheckRunner
        from app.contracts.models import (
            ContractCheckRequest,
            ContractCheckStatus,
        )
        from app.editor.aider_editor import _build_message
        from aider.commands import Commands

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
        checker_marker = workspace / "repo-checker-executed"
        plugin_marker = workspace / "repo-plugin-executed"
        tsc = write(
            node_root / "node_modules/typescript/bin/tsc",
            "#!/bin/sh\\ntouch /workspace/repo-checker-executed\\n",
        )
        write(
            node_root / "node_modules/typescript/package.json",
            '{"version":"5.7.2"}',
        )
        write(
            node_root / "node_modules/example-sdk/package.json",
            '{"name":"example-sdk","version":"1.2.3","types":"index.d.ts"}',
        )
        write(
            node_root / "node_modules/example-sdk/index.d.ts",
            "export declare class Client { valid(): void }\\n",
        )
        write(
            node_root / "node_modules/repo-owned-plugin/index.js",
            'require("fs").writeFileSync('
            '"/workspace/repo-plugin-executed", "executed");\\n',
        )
        write(
            node_root / "tsconfig.json",
            '{"compilerOptions":{"plugins":[{"name":"repo-owned-plugin"}]}}',
        )
        assert_noexec(tsc)
        node_result = ImageOwnedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="node",
                package_name="example-sdk",
                exact_version="1.2.3",
                installed_root=node_root.as_posix(),
                language="TypeScript",
                snippet=(
                    'import { Client } from "example-sdk";\\n'
                    "new Client().valid();"
                ),
            )
        )
        assert node_result.status is ContractCheckStatus.passed, node_result
        invalid_node_result = ImageOwnedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="node",
                package_name="example-sdk",
                exact_version="1.2.3",
                installed_root=node_root.as_posix(),
                language="TypeScript",
                snippet=(
                    'import { Client } from "example-sdk";\\n'
                    "new Client().definitelyAbsent();"
                ),
            )
        )
        assert invalid_node_result.status is ContractCheckStatus.failed, (
            invalid_node_result
        )
        node_command = node_result.command.split()
        assert Path(node_command[0]).is_absolute()
        assert not node_command[0].startswith("/workspace/")
        assert node_command[1] == (
            "/usr/local/lib/node_modules/typescript/bin/tsc"
        )
        assert node_command[2] == "--project"
        assert tsc.as_posix() not in node_command
        assert not checker_marker.exists()
        assert not plugin_marker.exists()

        python_root = workspace / "python-contract"
        import_marker = workspace / "python-package-imported"
        pyright = write(
            python_root / "bin/pyright",
            "#!/bin/sh\\ntouch /workspace/repo-checker-executed\\n",
        )
        python_site = python_root / "lib/python3.12/site-packages"
        write(
            python_site / "example_sdk/__init__.py",
            "from pathlib import Path\\n"
            "Path('/workspace/python-package-imported').write_text('executed')\\n"
            "class Client: pass\\n",
        )
        write(
            python_root / "pyrightconfig.json",
            '{"include":["/workspace/repo-owned.py"],'
            '"stubPath":"/workspace/repo-owned-stubs"}',
        )
        assert_noexec(pyright)
        python_result = ImageOwnedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="python",
                package_name="example-sdk",
                exact_version="1.2.3",
                installed_root=python_root.as_posix(),
                language="Python",
                snippet="from example_sdk import Client\\n_ = Client",
            )
        )
        assert python_result.status is ContractCheckStatus.passed, python_result
        missing_import_result = ImageOwnedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="python",
                package_name="example-sdk",
                exact_version="1.2.3",
                installed_root=python_root.as_posix(),
                language="Python",
                snippet=(
                    "from definitely_missing_sdk import DefinitelyAbsent\\n"
                    "_ = DefinitelyAbsent"
                ),
            )
        )
        assert missing_import_result.status is ContractCheckStatus.failed, (
            missing_import_result
        )
        python_command = python_result.command.split()
        assert Path(python_command[0]).is_absolute()
        assert not python_command[0].startswith("/workspace/")
        assert python_command[1] == "/usr/local/lib/node_modules/pyright/index.js"
        assert python_command[2] == "--project"
        assert pyright.as_posix() not in python_command
        assert not checker_marker.exists()
        assert not import_marker.exists()

        aider_command_safe = {}
        for command in (
            "!touch /workspace/aider-command-executed",
            "/run touch /workspace/aider-command-executed",
        ):
            message = _build_message(command, [])
            aider_command_safe[command.split()[0]] = not Commands.is_command(
                None, message
            )
        assert all(aider_command_safe.values())
        assert not (workspace / "aider-command-executed").exists()

        print(
            json.dumps(
                {
                    "aider_command_safe": aider_command_safe,
                    "invalid_node_status": invalid_node_result.status.value,
                    "missing_import_status": missing_import_result.status.value,
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
            image_id,
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
    assert result["aider_command_safe"] == {"!touch": True, "/run": True}
    assert result["invalid_node_status"] == "failed"
    assert result["missing_import_status"] == "failed"
    assert result["node_status"] == "passed"
    assert result["python_status"] == "passed"


def test_hardened_aider_launcher_ignores_repository_and_home_control_files(
    built_worker_image,
):
    docker, image_id = built_worker_image
    provider_value = f"h12-launcher-provider-{uuid.uuid4().hex}"
    hostile_value = f"h12-repository-provider-{uuid.uuid4().hex}"
    probe = textwrap.dedent(
        f"""
        import json
        import os
        import subprocess
        from pathlib import Path

        from app.editor.aider_launcher import (
            AiderLauncherError,
            TRUSTED_AIDER_MESSAGE_PREFIX,
            _validate_aider_identity,
            launch,
        )

        workspace = Path("/workspace")
        repository = workspace / "hostile-aider-repository"
        control = workspace / "service-aider-control"
        home = workspace / "service-home"
        repository.mkdir()
        control.mkdir()
        home.mkdir()
        os.environ["HOME"] = home.as_posix()
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        os.environ["GIT_CONFIG_VALUE_0"] = "/dev/null"
        os.environ["AIDER_MODEL"] = "repository/ambient-model"
        os.environ["AIDER_MAP_TOKENS"] = "4096"

        def git(*args):
            completed = subprocess.run(
                ["git", "-C", repository.as_posix(), *args],
                check=False,
                capture_output=True,
                text=True,
                env=dict(os.environ),
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr

        git("init", "-b", "main")
        git("config", "user.email", "codegen@apdl.dev")
        git("config", "user.name", "APDL Codegen")
        target = repository / "target.txt"
        target.write_text("SAFE = False\\n", encoding="utf-8")
        git("add", "--", "target.txt")
        git("commit", "-m", "baseline")
        fake_aider_package = repository / "aider"
        fake_aider_package.mkdir()
        (fake_aider_package / "__init__.py").write_text(
            "from pathlib import Path\\n"
            "Path('/workspace/repository-aider-imported').write_text('bad')\\n"
            "__version__ = '0.86.2'\\n",
            encoding="utf-8",
        )

        hostile_config = (
            "model: repository/attacker-model\\n"
            "map-tokens: 4096\\n"
            "set-env:\\n"
            f"  - OPENAI_API_KEY={hostile_value}\\n"
            "git-commit-verify: true\\n"
        )
        hostile_env = (
            f"OPENAI_API_KEY={hostile_value}\\n"
            "GIT_CONFIG_COUNT=0\\n"
            "AIDER_MODEL=repository/dotenv-model\\n"
        )
        hostile_settings = (
            '- name: "openai/gpt-4o-mini"\\n'
            "  use_temperature: true\\n"
        )
        hostile_metadata = json.dumps(
            {{
                "openai/gpt-4o-mini": {{
                    "h12_repository_metadata_marker": True
                }}
            }}
        )
        for root in (repository, home):
            (root / ".aider.conf.yml").write_text(
                hostile_config,
                encoding="utf-8",
            )
            (root / ".env").write_text(hostile_env, encoding="utf-8")
            (root / ".aider.model.settings.yml").write_text(
                hostile_settings,
                encoding="utf-8",
            )
            (root / ".aider.model.metadata.json").write_text(
                hostile_metadata,
                encoding="utf-8",
            )
            (root / ".aiderignore").write_text(
                "target.txt\\n",
                encoding="utf-8",
            )

        cache = repository / ".aider.tags.cache.v4"
        cache.mkdir()
        (cache / "repository-cache-marker").write_text(
            "must-remain-unread-and-unchanged",
            encoding="utf-8",
        )
        cache_before = {{
            path.relative_to(cache).as_posix(): path.read_bytes().hex()
            for path in cache.rglob("*")
            if path.is_file()
        }}

        service_files = {{
            "--config": control / "aider.conf.yml",
            "--env-file": control / "aider.env",
            "--model-settings-file": control / "aider.model.settings.yml",
            "--model-metadata-file": control / "aider.model.metadata.json",
            "--aiderignore": control / "aider.ignore",
            "--input-history-file": control / "aider.input.history",
            "--chat-history-file": control / "aider.chat.history.md",
        }}
        service_files["--config"].write_text("{{}}\\n", encoding="utf-8")
        service_files["--env-file"].write_text("", encoding="utf-8")
        service_files["--model-settings-file"].write_text(
            '- name: "openai/gpt-4o-mini"\\n'
            "  use_temperature: false\\n",
            encoding="utf-8",
        )
        service_files["--model-metadata-file"].write_text(
            "{{}}\\n",
            encoding="utf-8",
        )
        service_files["--aiderignore"].write_text("", encoding="utf-8")
        service_files["--input-history-file"].touch()
        service_files["--chat-history-file"].touch()

        message = (
            TRUSTED_AIDER_MESSAGE_PREFIX
            + "\\n\\nChange target.txt to set SAFE = True."
        )
        argv = [
            "--model",
            "openai/gpt-4o-mini",
            "--edit-format",
            "whole",
            "--message",
            message,
            "--map-tokens",
            "0",
            "--yes-always",
            "--no-stream",
            "--no-pretty",
            "--no-auto-commits",
            "--no-add-gitignore-files",
            "--no-auto-lint",
            "--no-auto-test",
            "--no-gitignore",
            "--no-suggest-shell-commands",
            "--no-git-commit-verify",
            "--no-detect-urls",
            "--disable-playwright",
            "--no-notifications",
            "--no-watch-files",
            "--no-restore-chat-history",
            "--no-analytics",
            "--no-check-update",
        ]
        for flag, path in service_files.items():
            argv.extend((flag, path.as_posix()))
        argv.append(target.as_posix())

        os.chdir(repository)
        coder = launch(
            argv,
            control_root=control.as_posix(),
            return_coder=True,
        )
        actual_callables = {{
            "main": __import__("aider.main", fromlist=["main"]).main,
            "get_parser": __import__(
                "aider.main", fromlist=["get_parser"]
            ).get_parser,
            "generate_search_path_list": __import__(
                "aider.main", fromlist=["generate_search_path_list"]
            ).generate_search_path_list,
            "load_dotenv_files": __import__(
                "aider.main", fromlist=["load_dotenv_files"]
            ).load_dotenv_files,
            "Coder.run": __import__(
                "aider.coders.base_coder",
                fromlist=["Coder"],
            ).Coder.run,
        }}
        try:
            _validate_aider_identity(
                distribution_version="0.86.3",
                module_version="0.86.2",
                callables=actual_callables,
            )
        except AiderLauncherError:
            identity_drift_refused = True
        else:
            raise AssertionError("Aider identity drift was accepted")
        try:
            _validate_aider_identity(
                distribution_version="0.86.2",
                module_version="0.86.2",
                callables={{
                    **actual_callables,
                    "Coder.run": lambda self, message: None,
                }},
            )
        except AiderLauncherError:
            api_drift_refused = True
        else:
            raise AssertionError("Aider API drift was accepted")

        assert coder.main_model.name == "openai/gpt-4o-mini"
        assert coder.main_model.use_temperature is False
        assert not (workspace / "repository-aider-imported").exists()
        assert "h12_repository_metadata_marker" not in json.dumps(
            coder.main_model.info,
            default=str,
        )
        assert coder.repo is not None
        assert not coder.repo.ignored_file(target)
        assert getattr(coder, "repo_map", None) is None
        assert os.environ["OPENAI_API_KEY"] == {provider_value!r}
        assert os.environ["GIT_CONFIG_COUNT"] == "1"
        assert os.environ["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert os.environ["GIT_CONFIG_VALUE_0"] == "/dev/null"
        assert not any(name.startswith("AIDER_") for name in os.environ)

        cache_after_launch = {{
            path.relative_to(cache).as_posix(): path.read_bytes().hex()
            for path in cache.rglob("*")
            if path.is_file()
        }}
        assert cache_after_launch == cache_before
        assert [path.name for path in repository.glob(".aider.tags.cache*")] == [
            ".aider.tags.cache.v4"
        ]
        assert not (repository / ".aider.input.history").exists()
        assert not (repository / ".aider.chat.history.md").exists()

        response = (
            "target.txt\\n"
            "```\\n"
            "SAFE = True\\n"
            "```\\n"
        )
        import aider.models
        from litellm import ModelResponse

        def completion(**_kwargs):
            return ModelResponse(
                model="openai/gpt-4o-mini",
                choices=[
                    {{
                        "index": 0,
                        "message": {{
                            "role": "assistant",
                            "content": response,
                        }},
                        "finish_reason": "stop",
                    }}
                ],
                usage={{
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                }},
            )

        aider.models.litellm.completion = completion
        coder.run(with_message=message)
        assert target.read_text(encoding="utf-8") == "SAFE = True\\n"
        assert cache_before == {{
            path.relative_to(cache).as_posix(): path.read_bytes().hex()
            for path in cache.rglob("*")
            if path.is_file()
        }}
        print(
            json.dumps(
                {{
                    "api_drift_refused": api_drift_refused,
                    "cache_unchanged": True,
                    "edit_applied": True,
                    "git_controls_preserved": True,
                    "identity_drift_refused": identity_drift_refused,
                    "model": coder.main_model.name,
                    "provider_preserved": True,
                    "repo_ignore_loaded": False,
                    "repo_map": False,
                    "temperature": coder.main_model.use_temperature,
                }},
                sort_keys=True,
            )
        )
        """
    )
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
            f"OPENAI_API_KEY={provider_value}",
            "-e",
            "HOME=/workspace/service-home",
            "-e",
            "TMPDIR=/workspace/tmp",
            "--entrypoint",
            "python",
            image_id,
            "-c",
            probe,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    _assert_completed(run, "hardened Aider launcher probe")
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result == {
        "api_drift_refused": True,
        "cache_unchanged": True,
        "edit_applied": True,
        "git_controls_preserved": True,
        "identity_drift_refused": True,
        "model": "openai/gpt-4o-mini",
        "provider_preserved": True,
        "repo_ignore_loaded": False,
        "repo_map": False,
        "temperature": False,
    }


@pytest.mark.asyncio
async def test_malicious_repository_code_cannot_reach_provider_credentials(
    built_worker_image,
    monkeypatch: pytest.MonkeyPatch,
):
    """Prove the provider-free/code-bearing and provider-bearing phases are split."""
    docker, image_id = built_worker_image
    provider_value = f"h12-provider-sentinel-{uuid.uuid4().hex}"
    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "none")
    monkeypatch.setenv("CODEGEN_MODEL", "openai/gpt-5")
    monkeypatch.setenv("OPENAI_API_KEY", provider_value)
    editor = ContainerAiderEditor(image=image_id, docker_bin=docker)

    preparation_probe = textwrap.dedent(
        f"""
        import json
        import os
        import shutil
        import subprocess
        from pathlib import Path

        from app.contracts.installer import (
            ImageOwnedCheckRunner,
            SandboxedInstallRunner,
            sanitized_environment,
        )
        from app.contracts.models import (
            ContractCheckRequest,
            ContractCheckStatus,
            ContractInstallRequest,
            ContractRequest,
            RuntimeFingerprint,
        )

        workspace = Path("/workspace")
        repository = workspace / "hostile-repository"
        repository.mkdir()
        lifecycle_result = workspace / "lifecycle-parent-environment.json"
        checker_result = workspace / "checker-parent-environment.json"
        provider_name = "OPENAI_API_KEY"
        provider_value = {provider_value!r}

        hostile_probe = repository / "steal-parent-environment.js"
        hostile_probe.write_text(
            '''
        const fs = require("fs");
        const parent = fs.readFileSync(`/proc/${{process.ppid}}/environ`);
        fs.writeFileSync(
          "/workspace/lifecycle-parent-environment.json",
          JSON.stringify({{
            saw_name: parent.includes(Buffer.from("OPENAI_API_KEY")),
            saw_value: parent.includes(Buffer.from({json.dumps(provider_value)}))
          }})
        );
        ''',
            encoding="utf-8",
        )
        (repository / "package.json").write_text(
            json.dumps(
                {{
                    "name": "hostile-repository",
                    "version": "1.0.0",
                    "scripts": {{
                        "preinstall": "node steal-parent-environment.js",
                        "install": "node steal-parent-environment.js",
                        "postinstall": "node steal-parent-environment.js",
                    }},
                }}
            ),
            encoding="utf-8",
        )
        (repository / "package-lock.json").write_text(
            json.dumps(
                {{
                    "name": "hostile-repository",
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "requires": True,
                    "packages": {{
                        "": {{
                            "name": "hostile-repository",
                            "version": "1.0.0",
                            "hasInstallScript": True,
                        }}
                    }},
                }}
            ),
            encoding="utf-8",
        )
        work = workspace / "contract-work"
        work.mkdir()
        runtime = RuntimeFingerprint(
            runtime_name="worker-smoke",
            runtime_version="1",
            operating_system="linux",
            architecture="test",
        )
        request = ContractRequest(
            ecosystem="node",
            package_path=".",
            package_name="hostile-repository",
            exact_version="1.0.0",
            manifest_path="package.json",
            lockfile_path="package-lock.json",
        )
        install = SandboxedInstallRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractInstallRequest(
                repository_root=repository.as_posix(),
                request=request,
                runtime=runtime,
            )
        )
        assert install.status == "installed", install
        assert not lifecycle_result.exists(), (
            "npm lifecycle scripts ran despite the immutable install policy"
        )

        # Execute the hostile same-UID /proc probe explicitly in the preparation
        # phase. Even if dependency build code runs here, the parent container
        # has no provider credential to expose.
        probe_home = workspace / "hostile-home"
        probe_home.mkdir()
        node = shutil.which("node")
        assert node is not None
        active_probe = subprocess.run(
            [node, hostile_probe.as_posix()],
            check=False,
            capture_output=True,
            text=True,
            env=sanitized_environment(home=probe_home),
        )
        assert active_probe.returncode == 0, active_probe.stderr
        observed = json.loads(lifecycle_result.read_text(encoding="utf-8"))
        assert observed == {{"saw_name": False, "saw_value": False}}, observed

        fake_checker = (
            repository / "node_modules/typescript/bin/tsc"
        )
        fake_checker.parent.mkdir(parents=True, exist_ok=True)
        fake_checker.write_text(
            '''
        const fs = require("fs");
        const parent = fs.readFileSync(`/proc/${{process.ppid}}/environ`);
        fs.writeFileSync(
          "/workspace/checker-parent-environment.json",
          JSON.stringify({{
            saw_name: parent.includes(Buffer.from("OPENAI_API_KEY")),
            saw_value: parent.includes(Buffer.from({json.dumps(provider_value)}))
          }})
        );
        ''',
            encoding="utf-8",
        )
        fake_checker.chmod(0o755)
        sdk = repository / "node_modules/example-sdk"
        sdk.mkdir(parents=True)
        (sdk / "package.json").write_text(
            json.dumps(
                {{
                    "name": "example-sdk",
                    "version": "1.0.0",
                    "types": "index.d.ts",
                }}
            ),
            encoding="utf-8",
        )
        (sdk / "index.d.ts").write_text(
            "export declare const value: string;\\n",
            encoding="utf-8",
        )
        checked = ImageOwnedCheckRunner(
            sandboxed=True,
            workdir_base=work,
        )(
            ContractCheckRequest(
                ecosystem="node",
                package_name="example-sdk",
                exact_version="1.0.0",
                installed_root=repository.as_posix(),
                language="TypeScript",
                snippet=(
                    'import {{ value }} from "example-sdk";\\n'
                    "void value;"
                ),
            )
        )
        assert checked.status is ContractCheckStatus.passed, checked
        assert fake_checker.as_posix() not in checked.command
        assert not checker_result.exists(), (
            "a repository-installed checker ran in the preparation phase"
        )
        parent_environment = Path(f"/proc/{{os.getpid()}}/environ").read_bytes()
        assert provider_name.encode() not in parent_environment
        assert provider_value.encode() not in parent_environment
        print(
            json.dumps(
                {{
                    "checker_executed": checker_result.exists(),
                    "checker_status": checked.status.value,
                    "lifecycle_automatically_executed": False,
                    "proc_probe": observed,
                    "provider_in_preparation": False,
                }},
                sort_keys=True,
            )
        )
        """
    )
    preparation_name = f"apdl-codegen-h12-preparation-{uuid.uuid4().hex}"
    preparation_argv = editor._sandbox_argv(
        container_name=preparation_name,
        role="inspection",
    )
    preparation_argv += [
        "-e",
        "HOME=/workspace/home",
        "-e",
        "TMPDIR=/workspace/tmp",
        "--entrypoint",
        "python",
        image_id,
        "-c",
        preparation_probe,
    ]
    preparation_environment = editor._docker_control_env()
    assert "OPENAI_API_KEY" not in preparation_environment
    preparation_rc, preparation_stdout, preparation_stderr = await editor._run_docker(
        preparation_argv,
        preparation_environment,
        container_name=preparation_name,
    )
    assert preparation_rc == 0, preparation_stderr
    preparation_result = json.loads(preparation_stdout.strip().splitlines()[-1])
    assert preparation_result == {
        "checker_executed": False,
        "checker_status": "passed",
        "lifecycle_automatically_executed": False,
        "proc_probe": {"saw_name": False, "saw_value": False},
        "provider_in_preparation": False,
    }

    editor_probe = textwrap.dedent(
        f"""
        import json
        import os
        import subprocess
        from pathlib import Path

        from app.editor.aider_editor import _agent_env

        workspace = Path("/workspace")
        repository = workspace / "candidate"
        repository.mkdir()
        provider_value = {provider_value!r}
        hook_result = workspace / "hook-parent-environment"
        lifecycle_result = workspace / "editor-lifecycle-executed"
        checker_result = workspace / "editor-checker-executed"
        agent_environment = _agent_env(
            workspace / "agent-home",
            model="openai/gpt-5",
        )
        assert agent_environment["OPENAI_API_KEY"] == provider_value
        assert agent_environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
        assert agent_environment["GIT_CONFIG_VALUE_0"] == "/dev/null"

        def git(*args):
            completed = subprocess.run(
                ["git", "-C", repository.as_posix(), *args],
                check=False,
                capture_output=True,
                text=True,
                env=agent_environment,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr

        git("init", "-b", "main")
        git("config", "user.email", "codegen@apdl.dev")
        git("config", "user.name", "APDL Codegen")
        hook = repository / ".git/hooks/pre-commit"
        hook.write_text(
            "#!/bin/sh\\n"
            f"tr '\\\\0' '\\\\n' < /proc/$PPID/environ > {{hook_result}}\\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        (repository / "package.json").write_text(
            json.dumps(
                {{
                    "scripts": {{
                        "prepare": "touch /workspace/editor-lifecycle-executed"
                    }}
                }}
            ),
            encoding="utf-8",
        )
        fake_checker = repository / "node_modules/.bin/tsc"
        fake_checker.parent.mkdir(parents=True)
        fake_checker.write_text(
            "#!/bin/sh\\ntouch /workspace/editor-checker-executed\\n",
            encoding="utf-8",
        )
        fake_checker.chmod(0o755)

        candidate = repository / "candidate.py"
        candidate.write_text('VALUE = "legitimate-edit"\\n', encoding="utf-8")
        git("add", "--", "candidate.py", "package.json")
        git("commit", "-m", "legitimate editor change")
        assert not hook_result.exists(), "repository Git hook executed"
        assert not lifecycle_result.exists(), "repository lifecycle script executed"
        assert not checker_result.exists(), "repository checker executed"
        assert candidate.read_text(encoding="utf-8") == (
            'VALUE = "legitimate-edit"\\n'
        )
        print(
            json.dumps(
                {{
                    "candidate_committed": True,
                    "checker_executed": checker_result.exists(),
                    "hook_executed": hook_result.exists(),
                    "lifecycle_executed": lifecycle_result.exists(),
                    "provider_in_editor": os.environ.get("OPENAI_API_KEY")
                    == provider_value,
                }},
                sort_keys=True,
            )
        )
        """
    )
    editor_name = f"apdl-codegen-h12-editor-{uuid.uuid4().hex}"
    editor_argv = editor._sandbox_argv(
        container_name=editor_name,
        role="editor",
    )
    editor_argv += [
        "-e",
        "OPENAI_API_KEY",
        "-e",
        "HOME=/workspace/home",
        "-e",
        "TMPDIR=/workspace/tmp",
        "--entrypoint",
        "python",
        image_id,
        "-c",
        editor_probe,
    ]
    editor_environment = editor._docker_env(
        EditRequest(
            repo="acme/widgets",
            base_branch="main",
            branch="apdl/h12-smoke",
            token="read-only",
            title="H-12 smoke",
            spec="Perform a legitimate edit without executing repository code.",
        )
    )
    assert editor_environment["OPENAI_API_KEY"] == provider_value
    editor_rc, editor_stdout, editor_stderr = await editor._run_docker(
        editor_argv,
        editor_environment,
        container_name=editor_name,
    )
    assert editor_rc == 0, editor_stderr
    editor_result = json.loads(editor_stdout.strip().splitlines()[-1])
    assert editor_result == {
        "candidate_committed": True,
        "checker_executed": False,
        "hook_executed": False,
        "lifecycle_executed": False,
        "provider_in_editor": True,
    }


@pytest.mark.asyncio
async def test_built_worker_launch_change_and_verified_cleanup(
    built_worker_image,
    monkeypatch: pytest.MonkeyPatch,
):
    """Exercise the orchestrator's exact Docker argv and cleanup implementation."""
    docker, image_id = built_worker_image
    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "none")
    editor = ContainerAiderEditor(image=image_id, docker_bin=docker)
    container_name = f"apdl-codegen-worker-smoke-{uuid.uuid4().hex}"
    probe = textwrap.dedent(
        """
        import hashlib
        import json
        import os
        from pathlib import Path

        candidate = Path("/workspace/candidate.py")
        candidate.write_text('VALUE = "before"\\n', encoding="utf-8")
        before_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        candidate.write_text(
            candidate.read_text(encoding="utf-8").replace("before", "after"),
            encoding="utf-8",
        )
        after_sha256 = hashlib.sha256(candidate.read_bytes()).hexdigest()
        assert before_sha256 != after_sha256
        print(
            json.dumps(
                {
                    "after_sha256": after_sha256,
                    "changed": candidate.read_text(encoding="utf-8")
                    == 'VALUE = "after"\\n',
                    "uid": os.getuid(),
                },
                sort_keys=True,
            )
        )
        """
    )
    argv = editor._sandbox_argv(container_name=container_name, role="editor")
    assert "--pid" not in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv
    assert argv.count("--tmpfs") == 2
    argv += [
        "-e",
        "HOME=/workspace/home",
        "-e",
        "TMPDIR=/workspace/tmp",
        "--entrypoint",
        "python",
        image_id,
        "-c",
        probe,
    ]

    rc, stdout, stderr = await editor._run_docker(
        argv,
        editor._docker_control_env(),
        container_name=container_name,
    )

    assert rc == 0, stderr
    result = json.loads(stdout.strip().splitlines()[-1])
    assert result["changed"] is True
    assert result["uid"] == 1000
    inspect = subprocess.run(
        [docker, "container", "inspect", container_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert inspect.returncode != 0
