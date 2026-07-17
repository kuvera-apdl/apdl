from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_DOCKERFILE = REPO_ROOT / "services/codegen/Dockerfile"


def test_controller_source_is_readable_by_an_overridden_runtime_uid() -> None:
    source = CONTROLLER_DOCKERFILE.read_text(encoding="utf-8")

    copy_index = source.index("COPY app/ app/")
    permission_index = source.index("chmod -R u=rwX,go=rX /app")
    user_index = source.index("USER appuser")

    assert copy_index < permission_index < user_index


def test_controller_installs_a_pinned_checksum_verified_docker_cli() -> None:
    source = CONTROLLER_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG DOCKER_CLI_VERSION=27.5.1" in source
    assert (
        "ARG DOCKER_CLI_SHA256_AMD64="
        "4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640"
    ) in source
    assert (
        "ARG DOCKER_CLI_SHA256_ARM64="
        "e6b53725a73763ab3f988c73f8772eaed429754c1a579db5ff11f21990fd1817"
    ) in source
    assert "download.docker.com/linux/static/stable/" in source
    assert "sha256sum -c -" in source
    assert "docker --version" in source
    assert "git docker.io curl" not in source
