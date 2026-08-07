"""Host broker-directory preparation must never chmod arbitrary paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.llm import broker_directory
from scripts import prepare_llm_broker_dir


ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
MAIN = (ROOT / "services/codegen/app/main.py").read_text(encoding="utf-8")


def test_make_targets_use_the_validated_broker_directory_preparer() -> None:
    invocation = ".venv/bin/python -m scripts.prepare_llm_broker_dir"
    for target in ("codegen-development-prepare:", "codegen-tenant-config:"):
        recipe = MAKEFILE.split(target, 1)[1].split("\n\n", 1)[0]
        assert invocation in recipe
        assert "chmod 0711" not in recipe


def test_controller_startup_prepares_root_before_opening_postgres() -> None:
    lifespan = MAIN.split("async def lifespan", 1)[1]
    assert lifespan.index("prepare_broker_root(") < lifespan.index(
        "asyncpg.create_pool("
    )


def test_controller_validates_github_recovery_before_opening_resources() -> None:
    lifespan = MAIN.split("async def lifespan", 1)[1]
    poll_interval = lifespan.index("poll_interval = codegen_ci_poll_interval()")
    validation = lifespan.index("github_webhook_secret(")

    assert poll_interval < validation
    assert validation < lifespan.index("prepare_broker_root(")
    assert validation < lifespan.index("asyncpg.create_pool(")


def test_preparer_rejects_root_without_changing_its_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = stat.S_IMODE(Path("/").stat().st_mode)
    monkeypatch.setenv("CODEGEN_LLM_BROKER_DIR", "/")

    with pytest.raises(ValueError, match="canonical safe absolute path"):
        prepare_llm_broker_dir.prepare_llm_broker_dir()

    assert stat.S_IMODE(Path("/").stat().st_mode) == before


def test_preparer_creates_only_the_missing_dedicated_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "apdl-codegen-llm-broker"
    monkeypatch.setattr(
        prepare_llm_broker_dir,
        "codegen_llm_broker_dir",
        lambda: path.as_posix(),
    )

    assert prepare_llm_broker_dir.prepare_llm_broker_dir() == path
    metadata = path.lstat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o711


def test_preparer_never_repairs_an_existing_directory_with_chmod(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "apdl-codegen-llm-broker"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    monkeypatch.setattr(
        prepare_llm_broker_dir,
        "codegen_llm_broker_dir",
        lambda: path.as_posix(),
    )

    with pytest.raises(RuntimeError, match="must already have mode 0711"):
        prepare_llm_broker_dir.prepare_llm_broker_dir()

    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_preparer_rejects_a_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o711)
    target.chmod(0o711)
    path = tmp_path / "apdl-codegen-llm-broker"
    path.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        prepare_llm_broker_dir,
        "codegen_llm_broker_dir",
        lambda: path.as_posix(),
    )

    with pytest.raises(RuntimeError, match="must be a real directory"):
        prepare_llm_broker_dir.prepare_llm_broker_dir()


def test_preparer_resolves_and_pins_a_safe_parent_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    lexical_parent = tmp_path / "parent-alias"
    lexical_parent.symlink_to(real_parent, target_is_directory=True)
    path = lexical_parent / "apdl-codegen-llm-broker"
    monkeypatch.setattr(
        prepare_llm_broker_dir,
        "codegen_llm_broker_dir",
        lambda: path.as_posix(),
    )

    assert prepare_llm_broker_dir.prepare_llm_broker_dir() == path
    assert (real_parent / path.name).is_dir()
    assert not path.is_symlink()


def test_preparer_rejects_a_non_sticky_writable_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    path = unsafe_parent / "apdl-codegen-llm-broker"
    monkeypatch.setattr(
        prepare_llm_broker_dir,
        "codegen_llm_broker_dir",
        lambda: path.as_posix(),
    )

    with pytest.raises(RuntimeError, match="must have the sticky bit"):
        prepare_llm_broker_dir.prepare_llm_broker_dir()

    assert not path.exists()


def test_parent_alias_swap_cannot_redirect_root_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_parent = tmp_path / "original-parent"
    alternate_parent = tmp_path / "alternate-parent"
    original_parent.mkdir(mode=0o700)
    alternate_parent.mkdir(mode=0o700)
    lexical_parent = tmp_path / "parent-alias"
    lexical_parent.symlink_to(original_parent, target_is_directory=True)
    path = lexical_parent / "apdl-codegen-llm-broker"
    monkeypatch.setattr(
        prepare_llm_broker_dir,
        "codegen_llm_broker_dir",
        lambda: path.as_posix(),
    )
    real_mkdir = os.mkdir
    swapped = False

    def swap_then_mkdir(
        name: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if name == path.name and dir_fd is not None and not swapped:
            lexical_parent.unlink()
            lexical_parent.symlink_to(alternate_parent, target_is_directory=True)
            swapped = True
        real_mkdir(name, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(broker_directory.os, "mkdir", swap_then_mkdir)

    assert prepare_llm_broker_dir.prepare_llm_broker_dir() == path
    assert swapped is True
    assert (original_parent / path.name).is_dir()
    assert not (alternate_parent / path.name).exists()
