from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_COMPOSE = REPO_ROOT / "infra/docker/docker-compose.yml"
ROLLOUT_COMPOSE = REPO_ROOT / "infra/docker/docker-compose.codegen-rollout.yml"
DOCKER = shutil.which("docker")


def _compose_config(*files: Path, environment: dict[str, str]) -> dict:
    if DOCKER is None:
        pytest.skip("Docker Compose is required for Compose contract tests")
    command = [DOCKER, "compose"]
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
            "CODEGEN_ROLLOUT_STAGE": "reviewed_pr",
            "CODEGEN_ROLLOUT_AUTHORIZATION_PATH": "/stale/publication-bundle.json",
        }
    )

    config = _compose_config(BASE_COMPOSE, environment=environment)
    codegen_environment = config["services"]["codegen"]["environment"]

    assert codegen_environment["CODEGEN_ROLLOUT_STAGE"] == "offline"
    assert codegen_environment["CODEGEN_ROLLOUT_AUTHORIZATION_PATH"] == ""


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
