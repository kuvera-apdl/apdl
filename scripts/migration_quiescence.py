#!/usr/bin/env python3
"""Fail closed when APDL Compose services that can race a migration are live."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence


SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


class QuiescenceError(RuntimeError):
    """The Docker state does not prove that the requested services are stopped."""


def _run(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise QuiescenceError(
            f"Could not inspect Docker migration quiescence{suffix}"
        ) from exc
    return result.stdout


def _compose_project(anchor_container: str) -> str:
    project = _run(
        (
            "docker",
            "inspect",
            "--format",
            f'{{{{ index .Config.Labels "{PROJECT_LABEL}" }}}}',
            anchor_container,
        )
    ).strip()
    if not project or project == "<no value>":
        raise QuiescenceError(
            "Migration anchor container is not owned by Docker Compose; "
            "service quiescence cannot be proven"
        )
    return project


def _running_project_services(project: str) -> dict[str, tuple[str, ...]]:
    output = _run(
        (
            "docker",
            "ps",
            "--filter",
            f"label={PROJECT_LABEL}={project}",
            "--format",
            f'{{{{.ID}}}}\t{{{{.Label "{SERVICE_LABEL}"}}}}',
        )
    )
    containers: dict[str, list[str]] = {}
    for line in output.splitlines():
        container_id, separator, service = line.partition("\t")
        container_id = container_id.strip()
        service = service.strip()
        if not separator or not container_id or not service:
            raise QuiescenceError(
                f"Could not parse Docker Compose service state: {line!r}"
            )
        containers.setdefault(service, []).append(container_id)
    return {
        service: tuple(sorted(container_ids))
        for service, container_ids in containers.items()
    }


def assert_services_stopped(
    anchor_container: str,
    forbidden_services: Sequence[str],
) -> None:
    if not anchor_container:
        raise QuiescenceError("Migration anchor container is required")
    services = tuple(dict.fromkeys(forbidden_services))
    if not services:
        raise QuiescenceError("At least one service must be checked")
    invalid = tuple(service for service in services if not SERVICE_NAME.fullmatch(service))
    if invalid:
        raise QuiescenceError(
            f"Invalid Docker Compose service name: {', '.join(invalid)}"
        )

    project = _compose_project(anchor_container)
    running = _running_project_services(project)
    conflicts = tuple(service for service in services if service in running)
    if not conflicts:
        return

    detail = ", ".join(
        f"{service} ({', '.join(running[service])})" for service in conflicts
    )
    raise QuiescenceError(
        "Migration requires stopped application services in Compose project "
        f"{project!r}; still running: {detail}. Stop them and retry."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor-container", required=True)
    parser.add_argument("--service", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        assert_services_stopped(args.anchor_container, args.service)
    except QuiescenceError as exc:
        print(f"Migration quiescence check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
