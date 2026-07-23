"""Strict invocation tests for the image-owned Aider adapter."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from app.editor.aider_launcher import (
    AiderLauncherError,
    LITELLM_VERSION,
    TRUSTED_AIDER_MESSAGE_PREFIX,
    _AIDER_LITELLM_EXCEPTION_NAMES,
    _EXPECTED_EXPORTED_LITELLM_ERRORS,
    _UNMAPPED_LITELLM_EXCEPTION,
    _install_litellm_exception_compatibility,
    _validate_aider_identity,
    _validate_litellm_compatibility,
    validate_invocation,
)


def _control_files(root: Path) -> dict[str, Path]:
    root.mkdir()
    files = {
        "--config": root / "aider.conf.yml",
        "--env-file": root / "aider.env",
        "--model-settings-file": root / "aider.model.settings.yml",
        "--model-metadata-file": root / "aider.model.metadata.json",
        "--aiderignore": root / "aider.ignore",
        "--input-history-file": root / "aider.input.history",
        "--chat-history-file": root / "aider.chat.history.md",
    }
    files["--config"].write_text("{}\n", encoding="utf-8")
    files["--env-file"].write_text("", encoding="utf-8")
    files["--model-settings-file"].write_text(
        '- name: "openai/gpt-5"\n  use_temperature: false\n',
        encoding="utf-8",
    )
    files["--model-metadata-file"].write_text("{}\n", encoding="utf-8")
    files["--aiderignore"].write_text("", encoding="utf-8")
    files["--input-history-file"].touch()
    files["--chat-history-file"].touch()
    return files


def _argv(files: dict[str, Path]) -> list[str]:
    argv = [
        "--model",
        "openai/gpt-5",
        "--message",
        f"{TRUSTED_AIDER_MESSAGE_PREFIX}\n\nMake the edit.",
        "--map-tokens",
        "0",
        "--disable-playwright",
        "--no-add-gitignore-files",
        "--no-analytics",
        "--no-auto-lint",
        "--no-auto-test",
        "--no-check-update",
        "--no-detect-urls",
        "--no-git-commit-verify",
        "--no-gitignore",
        "--no-notifications",
        "--no-restore-chat-history",
        "--no-suggest-shell-commands",
        "--no-watch-files",
    ]
    for flag, path in files.items():
        argv.extend((flag, path.as_posix()))
    return argv


def test_launcher_accepts_only_disjoint_service_control_paths(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    control = tmp_path / "control"
    files = _control_files(control)

    invocation = validate_invocation(
        _argv(files),
        control_root=control.as_posix(),
        cwd=repository,
    )

    assert invocation.control_root == control.resolve()
    assert invocation.environment_file == files["--env-file"].resolve()
    assert invocation.message.startswith(TRUSTED_AIDER_MESSAGE_PREFIX)


def test_launcher_rejects_repo_config_and_command_capable_messages(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    control = tmp_path / "control"
    files = _control_files(control)
    repo_config = repository / ".aider.conf.yml"
    repo_config.write_text("model: attacker/model\n", encoding="utf-8")
    argv = _argv(files)
    argv[argv.index("--config") + 1] = repo_config.as_posix()

    with pytest.raises(AiderLauncherError, match="escaped"):
        validate_invocation(
            argv,
            control_root=control.as_posix(),
            cwd=repository,
        )

    argv = _argv(files)
    argv[argv.index("--message") + 1] = "!touch /tmp/executed"
    with pytest.raises(AiderLauncherError, match="inert prefix"):
        validate_invocation(
            argv,
            control_root=control.as_posix(),
            cwd=repository,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (("--map-tokens", "1024"), "repository map"),
        (("--load", "/tmp/commands"), "Forbidden"),
        (("--set-env", "OPENAI_API_KEY=attacker"), "Forbidden"),
    ],
)
def test_launcher_rejects_cache_and_control_bypass_options(
    tmp_path,
    mutation,
    message,
):
    repository = tmp_path / "repo"
    repository.mkdir()
    control = tmp_path / "control"
    files = _control_files(control)
    argv = _argv(files)
    flag, value = mutation
    if flag in argv:
        argv[argv.index(flag) + 1] = value
    else:
        argv.extend((flag, value))

    with pytest.raises(AiderLauncherError, match=message):
        validate_invocation(
            argv,
            control_root=control.as_posix(),
            cwd=repository,
        )


def _aider_callables() -> dict[str, object]:
    def main(argv=None, input=None, output=None, force_git_root=None, return_coder=False):
        return None

    def get_parser(default_config_files, git_root):
        return None

    def generate_search_path_list(default_file, git_root, command_line_file):
        return None

    def load_dotenv_files(git_root, dotenv_fname, encoding="utf-8"):
        return None

    def coder_run(self, with_message=None, preproc=True):
        return None

    def exception_load(self, strict=False):
        return None

    return {
        "main": main,
        "get_parser": get_parser,
        "generate_search_path_list": generate_search_path_list,
        "load_dotenv_files": load_dotenv_files,
        "Coder.run": coder_run,
        "LiteLLMExceptions._load": exception_load,
    }


def test_launcher_fails_closed_on_aider_identity_or_api_drift():
    callables = _aider_callables()
    _validate_aider_identity(
        distribution_version="0.86.2",
        module_version="0.86.2",
        callables=callables,
    )

    with pytest.raises(AiderLauncherError, match="identity"):
        _validate_aider_identity(
            distribution_version="0.86.3",
            module_version="0.86.2",
            callables=callables,
        )

    def drifted_main(argv=None):
        return None

    with pytest.raises(AiderLauncherError, match="API mismatch: main"):
        _validate_aider_identity(
            distribution_version="0.86.2",
            module_version="0.86.2",
            callables={**callables, "main": drifted_main},
        )


def _fake_litellm() -> ModuleType:
    module = ModuleType("litellm")
    names = _AIDER_LITELLM_EXCEPTION_NAMES | {
        _UNMAPPED_LITELLM_EXCEPTION
    }
    for name in names:
        setattr(module, name, type(name, (Exception,), {}))
    module.ErrorEventError = type("ErrorEventError", (object,), {})
    return module


def _fake_aider_exceptions() -> ModuleType:
    module = ModuleType("aider.exceptions")

    class LiteLLMExceptions:
        exceptions: dict[type[BaseException], object] = {}
        exception_info = {
            name: object() for name in _AIDER_LITELLM_EXCEPTION_NAMES
        }

        def __init__(self):
            self._load()

        def _load(self, strict=False):
            raise AssertionError("unpatched Aider exception discovery ran")

        def exceptions_tuple(self):
            return tuple(self.exceptions)

    module.LiteLLMExceptions = LiteLLMExceptions
    return module


def test_litellm_185_exception_compatibility_is_exact_and_unmapped():
    litellm = _fake_litellm()
    aider_exceptions = _fake_aider_exceptions()
    _validate_litellm_compatibility(
        distribution_version=LITELLM_VERSION,
        aider_exception_names=_AIDER_LITELLM_EXCEPTION_NAMES,
        exported_exception_names=_EXPECTED_EXPORTED_LITELLM_ERRORS,
    )

    _install_litellm_exception_compatibility(
        aider_exceptions,
        litellm,
        distribution_version=LITELLM_VERSION,
    )
    mapped = aider_exceptions.LiteLLMExceptions().exceptions_tuple()

    assert getattr(litellm, _UNMAPPED_LITELLM_EXCEPTION) not in mapped
    assert {
        getattr(litellm, name)
        for name in _AIDER_LITELLM_EXCEPTION_NAMES
    } == set(mapped)

    litellm.UnexpectedProviderError = type(
        "UnexpectedProviderError",
        (Exception,),
        {},
    )
    with pytest.raises(AiderLauncherError, match="exception inventory"):
        aider_exceptions.LiteLLMExceptions()

    with pytest.raises(AiderLauncherError, match="identity mismatch"):
        _validate_litellm_compatibility(
            distribution_version="1.85.1",
            aider_exception_names=_AIDER_LITELLM_EXCEPTION_NAMES,
            exported_exception_names=_EXPECTED_EXPORTED_LITELLM_ERRORS,
        )

    with pytest.raises(AiderLauncherError, match="Aider exception inventory"):
        _validate_litellm_compatibility(
            distribution_version=LITELLM_VERSION,
            aider_exception_names=_AIDER_LITELLM_EXCEPTION_NAMES
            - {"APIError"},
            exported_exception_names=_EXPECTED_EXPORTED_LITELLM_ERRORS,
        )
