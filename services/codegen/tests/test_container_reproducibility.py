from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CODEGEN_DIR = REPO_ROOT / "services" / "codegen"
PINNED_PYTHON = (
    "python:3.12-slim@sha256:"
    "423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
)


def test_worker_uses_immutable_images_snapshot_packages_and_hash_lock() -> None:
    source = (CODEGEN_DIR / "Dockerfile.worker").read_text(encoding="utf-8")

    assert source.count(f"FROM {PINNED_PYTHON}") == 2
    assert source.startswith("# APDL codegen agent sandbox")
    assert "FROM node:20-bookworm-slim@sha256:" in source
    assert "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in source
    assert "COPY requirements-agent.lock /tmp/requirements-agent.lock" in source
    assert "--require-hashes -r /tmp/requirements-agent.lock" in source
    assert "pip install --no-cache-dir uv" not in source
    assert "deb.nodesource.com" not in source


def test_codegen_locks_cover_runtime_and_worker_tools() -> None:
    runtime_lock = (CODEGEN_DIR / "requirements.lock").read_text(encoding="utf-8")
    worker_lock = (CODEGEN_DIR / "requirements-agent.lock").read_text(
        encoding="utf-8"
    )

    assert "--hash=sha256:" in runtime_lock
    assert "--hash=sha256:" in worker_lock
    assert "aider-chat==0.86.2" in worker_lock
    assert "uv==0.11.0" in worker_lock


def test_egress_proxy_uses_immutable_base_and_package_snapshot() -> None:
    source = (
        REPO_ROOT / "infra" / "docker" / "codegen-egress" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert source.startswith("FROM debian:trixie-slim@sha256:")
    assert "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in source
    for package in ("ca-certificates", "curl", "socat", "squid", "tini"):
        assert f'"{package}=${{' in source
