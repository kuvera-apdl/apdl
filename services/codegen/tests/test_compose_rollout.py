from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_COMPOSE = REPO_ROOT / "infra/docker/docker-compose.yml"
DEVELOPMENT_COMPOSE = (
    REPO_ROOT / "infra/docker/docker-compose.codegen-development.yml"
)
ROLLOUT_COMPOSE = REPO_ROOT / "infra/docker/docker-compose.codegen-rollout.yml"
DOCKER = shutil.which("docker")
MAKE = shutil.which("make")


def _compose_config(*files: Path, environment: dict[str, str]) -> dict:
    if DOCKER is None:
        pytest.skip("Docker Compose is required for Compose contract tests")
    command = [DOCKER, "compose", "--profile", "codegen"]
    for path in files:
        command.extend(("-f", str(path)))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_base_compose_cannot_be_promoted_by_ambient_rollout_environment() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "CODEGEN_DEVELOPMENT_MODE": "true",
            "CODEGEN_ROLLOUT_STAGE": "reviewed_pr",
            "CODEGEN_ROLLOUT_AUTHORIZATION_PATH": "/stale/publication-bundle.json",
        }
    )

    config = _compose_config(BASE_COMPOSE, environment=environment)
    codegen_environment = config["services"]["codegen"]["environment"]

    assert codegen_environment["CODEGEN_ROLLOUT_STAGE"] == "offline"
    assert codegen_environment["CODEGEN_ROLLOUT_AUTHORIZATION_PATH"] == ""
    assert "CODEGEN_DEVELOPMENT_MODE" not in codegen_environment


def test_development_overlay_wires_the_local_sandbox_runtime() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "CODEGEN_DEVELOPMENT_DOCKER_GID": "1000",
            "CODEGEN_DEVELOPMENT_DOCKER_SOCKET": "/var/run/docker.sock",
            "CODEGEN_DEVELOPMENT_DOCKER_SOCKET_GID": "999",
            "CODEGEN_DEVELOPMENT_DOCKER_UID": "1000",
            "CODEGEN_DEVELOPMENT_SANDBOX_IMAGE": (
                "apdl-codegen-sandbox:local-development"
            ),
            "CODEGEN_DEVELOPMENT_SANDBOX_NETWORK": "apdl-codegen-development",
            # Neither stale publication value may alter this explicit local mode.
            "CODEGEN_ROLLOUT_STAGE": "reviewed_pr",
            "CODEGEN_ROLLOUT_AUTHORIZATION_PATH": "/stale/bundle.json",
        }
    )

    config = _compose_config(
        BASE_COMPOSE,
        DEVELOPMENT_COMPOSE,
        environment=environment,
    )
    codegen = config["services"]["codegen"]
    codegen_environment = codegen["environment"]

    assert codegen["user"] == "1000:1000"
    assert codegen["group_add"] == ["999"]
    assert codegen_environment["CODEGEN_DEVELOPMENT_MODE"] == "true"
    assert codegen_environment["CODEGEN_REVISION"] == "local-development"
    assert codegen_environment["CODEGEN_ROLLOUT_STAGE"] == "development_pr"
    assert codegen_environment["CODEGEN_ROLLOUT_AUTHORIZATION_PATH"] == ""
    assert codegen_environment["CODEGEN_SANDBOX"] == "docker"
    assert codegen_environment["CODEGEN_SANDBOX_IMAGE"] == (
        "apdl-codegen-sandbox:local-development"
    )
    assert codegen_environment["CODEGEN_SANDBOX_NETWORK"] == (
        "apdl-codegen-development"
    )
    assert codegen_environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert {
        "type": "bind",
        "source": "/var/run/docker.sock",
        "target": "/var/run/docker.sock",
    } in codegen["volumes"]
    assert all(
        volume["target"] != "/run/apdl/codegen/publication-bundle.json"
        for volume in codegen["volumes"]
    )
    assert codegen["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-fsS",
        "http://127.0.0.1:8084/ready",
    ]


def test_dev_all_starts_codegen_offline_without_publication_overlay() -> None:
    if MAKE is None:
        pytest.skip("make is required for orchestration contract tests")

    completed = subprocess.run(
        [MAKE, "-n", "dev-all"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    rendered = completed.stdout
    assert "CODEGEN_DEVELOPMENT_DOCKER_SOCKET=" not in rendered
    assert "docker-compose.codegen-development.yml" not in rendered
    assert "apdl-codegen-sandbox:local-development" not in rendered
    assert "--profile agents --profile codegen up -d --build --wait" in rendered
    assert "agents codegen" in rendered


def test_development_prepare_resolves_the_active_unix_docker_host() -> None:
    if MAKE is None:
        pytest.skip("make is required for orchestration contract tests")

    environment = os.environ.copy()
    environment.pop("CODEGEN_DEVELOPMENT_DOCKER_SOCKET", None)
    environment["DOCKER_HOST"] = "unix:///tmp/custom-docker.sock"
    completed = subprocess.run(
        [
            MAKE,
            "-s",
            "-f",
            str(REPO_ROOT / "Makefile"),
            "-f",
            "-",
            "print-codegen-development-socket",
        ],
        cwd=REPO_ROOT,
        env=environment,
        input=(
            "print-codegen-development-socket:\n"
            "\t@printf '%s\\n' \"$(CODEGEN_DEVELOPMENT_DOCKER_SOCKET)\"\n"
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "/tmp/custom-docker.sock"


def test_dev_script_delegates_full_stack_lifecycle_to_make() -> None:
    source = (REPO_ROOT / "scripts/dev.sh").read_text(encoding="utf-8")
    up_body = source.split("cmd_up_full() {", 1)[1].split("\n}", 1)[0]
    down_body = source.split("cmd_down() {", 1)[1].split("\n}", 1)[0]

    assert 'make -C "$ROOT_DIR" --no-print-directory dev-all' in up_body
    assert "dc_full up" not in up_body
    assert 'make -C "$ROOT_DIR" --no-print-directory dev-down' in down_body
    assert "dc_full down" not in down_body


def test_reviewed_overlay_injects_evaluated_publication_identity() -> None:
    controller_image = "sha256:" + "a" * 64
    candidate_image = "sha256:" + "b" * 64
    environment = os.environ.copy()
    environment.pop("CODEGEN_ROLLOUT_STAGE", None)
    environment.update(
        {
            "CODEGEN_CONTROLLER_IMAGE": controller_image,
            "CODEGEN_DOCKER_GID": "1000",
            "CODEGEN_DOCKER_SOCKET": "/var/run/docker.sock",
            "CODEGEN_DOCKER_SOCKET_GID": "1000",
            "CODEGEN_DOCKER_UID": "1000",
            "CODEGEN_REVISION": "evaluated-revision",
            "CODEGEN_ROLLOUT_BUNDLE_PATH": "/tmp/publication-bundle.json",
            "CODEGEN_SANDBOX_IMAGE": candidate_image,
            "CODEGEN_SANDBOX_NETWORK": "codegen-egress-filtered",
        }
    )

    config = _compose_config(
        BASE_COMPOSE,
        ROLLOUT_COMPOSE,
        environment=environment,
    )
    codegen = config["services"]["codegen"]
    codegen_environment = codegen["environment"]

    assert codegen["image"] == controller_image
    assert codegen_environment["CODEGEN_ROLLOUT_STAGE"] == "reviewed_pr"
    assert codegen_environment["CODEGEN_ROLLOUT_AUTHORIZATION_PATH"] == (
        "/run/apdl/codegen/publication-bundle.json"
    )
    assert codegen_environment["CODEGEN_CONTROLLER_IMAGE_ID"] == controller_image
    assert codegen_environment["CODEGEN_SANDBOX_IMAGE"] == candidate_image
    assert codegen_environment["CODEGEN_REVISION"] == "evaluated-revision"
