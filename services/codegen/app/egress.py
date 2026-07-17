"""Attested Unix-socket egress boundary for network-none Codegen workers."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from types import FrameType
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


EGRESS_POLICY_LABEL = "dev.apdl.codegen.egress.policy-sha256"
EGRESS_ROLE_LABEL = "dev.apdl.codegen.egress.role"
EGRESS_SOCKET_VOLUME_LABEL = "dev.apdl.codegen.egress.socket-volume"
EGRESS_PROXY_ROLE = "proxy"
EGRESS_SOCKET_ROLE = "socket"
EGRESS_TRANSPORT = "network_none_unix_socket@1"
EGRESS_PROXY_HOST = "127.0.0.1"
EGRESS_PROXY_PORT = 3128
EGRESS_PROXY_URL = f"http://{EGRESS_PROXY_HOST}:{EGRESS_PROXY_PORT}"
EGRESS_SOCKET_DIR = "/run/apdl-codegen-egress"
EGRESS_SOCKET_PATH = f"{EGRESS_SOCKET_DIR}/proxy.sock"
EGRESS_PROXY_ENTRYPOINT = (
    "/usr/bin/tini",
    "--",
    "/usr/local/bin/codegen-egress-proxy",
)
EGRESS_PROXY_USER = "proxy"
EGRESS_PROXY_HEALTHCHECK: dict[str, Any] = {
    "Test": ["CMD", "/usr/local/bin/codegen-egress-healthcheck"],
    "Interval": 5_000_000_000,
    "Timeout": 3_000_000_000,
    "StartPeriod": 5_000_000_000,
    "Retries": 20,
}
EGRESS_PROXY_TMPFS: dict[str, str] = {
    "/tmp": "rw,nosuid,nodev,noexec,size=32m,mode=1777",
    "/var/log/squid": "rw,nosuid,nodev,noexec,size=16m,uid=13,gid=13",
    "/var/spool/squid": "rw,nosuid,nodev,noexec,size=16m,uid=13,gid=13",
}
EGRESS_PROXY_ENV: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_NO_PROXY = "localhost,127.0.0.1,::1"
_DENIED_PROXY_CONNECT_TARGETS: tuple[str, ...] = (
    "169.254.169.254:443",
    "10.0.0.1:443",
    "172.16.0.1:443",
    "192.168.0.1:443",
)
_DENIED_PROXY_HTTP_TARGETS: tuple[str, ...] = (
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "http://172.16.0.1/",
    "http://192.168.0.1/",
)
_ALLOWED_PROXY_CONNECT_TARGET = "api.github.com:443"
_DIRECT_PUBLIC_TARGET = ("1.1.1.1", 443)
_DIRECT_PRIVATE_TARGET = ("169.254.169.254", 80)
_DIRECT_DNS_TARGET = ("8.8.8.8", 53)
_EXPECTED_DIRECT_BLOCK_ERRNOS = frozenset(
    {
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ETIMEDOUT,
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_VOLUME_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_DOCKER_OUTPUT = 1024 * 1024

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DockerImageId = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EgressProbeResult(_StrictModel):
    schema_version: Literal["codegen_egress_probe@3"] = "codegen_egress_probe@3"
    transport: Literal["network_none_unix_socket@1"] = EGRESS_TRANSPORT
    policy_sha256: Sha256
    proxy_http_denials: dict[str, Literal[403]]
    proxy_connect_denials: dict[str, Literal[403]]
    allowed_connect_status: Literal[200]
    direct_public_blocked: Literal[True]
    direct_private_blocked: Literal[True]
    direct_dns_blocked: Literal[True]

    @model_validator(mode="after")
    def exact_denial_targets(self) -> EgressProbeResult:
        if set(self.proxy_http_denials) != set(_DENIED_PROXY_HTTP_TARGETS):
            raise ValueError(
                "egress probe result does not cover every denied HTTP target"
            )
        if set(self.proxy_connect_denials) != set(
            _DENIED_PROXY_CONNECT_TARGETS
        ):
            raise ValueError(
                "egress probe result does not cover every denied CONNECT target"
            )
        return self


class EgressPolicyAttestation(_StrictModel):
    schema_version: Literal["codegen_egress_attestation@3"] = (
        "codegen_egress_attestation@3"
    )
    transport: Literal["network_none_unix_socket@1"] = EGRESS_TRANSPORT
    launch_id: str = Field(min_length=1, max_length=200)
    policy_sha256: Sha256
    socket_volume: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    proxy_container_id: str = Field(min_length=1)
    proxy_image_id: DockerImageId
    probe_image_id: DockerImageId
    proxy_url: Literal["http://127.0.0.1:3128"] = EGRESS_PROXY_URL
    probe: EgressProbeResult

    def evidence_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_policy_sha256(value: str) -> str:
    normalized = value.strip()
    if not _SHA256.fullmatch(normalized):
        raise ValueError(
            "CODEGEN_EGRESS_POLICY_SHA256 must be 64 lowercase hexadecimal characters"
        )
    return normalized


def validate_proxy_image_id(value: str) -> str:
    normalized = value.strip()
    if not _IMAGE_ID.fullmatch(normalized):
        raise ValueError("Codegen egress image must be an immutable sha256 ID")
    return normalized


def validate_socket_volume(value: str) -> str:
    normalized = value.strip()
    if not _VOLUME_NAME.fullmatch(normalized):
        raise ValueError(
            "CODEGEN_EGRESS_SOCKET_VOLUME must be a canonical Docker volume name"
        )
    return normalized


def validate_proxy_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            f"Codegen workers require the canonical proxy URL {EGRESS_PROXY_URL}"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != EGRESS_PROXY_HOST
        or port != EGRESS_PROXY_PORT
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"Codegen workers require the canonical proxy URL {EGRESS_PROXY_URL}"
        )
    return EGRESS_PROXY_URL


def proxy_environment(proxy_url: str = EGRESS_PROXY_URL) -> dict[str, str]:
    canonical = validate_proxy_url(proxy_url)
    return {
        "HTTP_PROXY": canonical,
        "HTTPS_PROXY": canonical,
        "ALL_PROXY": canonical,
        "NO_PROXY": _NO_PROXY,
        "http_proxy": canonical,
        "https_proxy": canonical,
        "all_proxy": canonical,
        "no_proxy": _NO_PROXY,
    }


def inherited_proxy_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if source is None else source
    present = {name: source[name] for name in EGRESS_PROXY_ENV if name in source}
    if not present:
        return {}
    expected = proxy_environment()
    if present != expected:
        raise ValueError("Codegen proxy environment is incomplete or non-canonical")
    return expected


def worker_socket_mount(socket_volume: str) -> str:
    """Return the canonical read-only Docker mount for one evaluated worker."""
    return (
        f"type=volume,src={validate_socket_volume(socket_volume)},"
        f"dst={EGRESS_SOCKET_DIR},readonly"
    )


def relay_command(command: Sequence[str]) -> list[str]:
    """Wrap a worker command with the sealed loopback-to-Unix proxy relay."""
    if not command or any(not item or "\x00" in item for item in command):
        raise ValueError("egress relay requires a canonical non-empty command")
    return [
        "-m",
        "app.egress",
        "relay-exec",
        "--socket-path",
        EGRESS_SOCKET_PATH,
        "--listen-host",
        EGRESS_PROXY_HOST,
        "--listen-port",
        str(EGRESS_PROXY_PORT),
        "--",
        *command,
    ]


def _strict_json(value: str) -> Any:
    def pairs(items):
        out = {}
        for key, item in items:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = item
        return out

    def constant(raw: str):
        raise ValueError(f"non-finite JSON value: {raw}")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)


def _single_inspect(payload: Any, *, label: str) -> dict[str, Any]:
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise ValueError(f"Docker {label} inspection must return exactly one object")
    return payload[0]


def _empty(value: Any) -> bool:
    return value is None or value == [] or value == {}


def select_proxy_container_id(output: str) -> str:
    ids = [line.strip() for line in output.splitlines() if line.strip()]
    if len(ids) != 1:
        raise ValueError("exactly one running attested egress proxy is required")
    return ids[0]


def validate_socket_volume_exclusivity(
    output: str,
    *,
    expected_proxy_container_id: str,
) -> None:
    """Require the proxy to be the volume's only running consumer."""
    ids = [line.strip() for line in output.splitlines() if line.strip()]
    if len(ids) != len(set(ids)):
        raise ValueError("Docker returned duplicate egress volume consumers")
    if set(ids) != {expected_proxy_container_id}:
        raise ValueError(
            "Codegen egress socket volume must be mounted only by the "
            "attested proxy before worker launch"
        )


def validate_socket_volume_inspect(
    payload: Any,
    *,
    expected_name: str,
    expected_policy_sha256: str,
) -> None:
    volume = _single_inspect(payload, label="volume")
    labels = volume.get("Labels")
    if volume.get("Name") != expected_name:
        raise ValueError("Docker egress socket volume name does not match")
    if volume.get("Driver") != "local" or volume.get("Scope") != "local":
        raise ValueError("Codegen egress socket must be a local Docker volume")
    if not _empty(volume.get("Options")):
        raise ValueError("Codegen egress socket volume must not have driver options")
    if not isinstance(labels, dict):
        raise ValueError("Codegen egress socket volume has no attestation labels")
    if labels.get(EGRESS_ROLE_LABEL) != EGRESS_SOCKET_ROLE:
        raise ValueError("Codegen egress socket volume role label does not match")
    if labels.get(EGRESS_POLICY_LABEL) != expected_policy_sha256:
        raise ValueError("Codegen egress socket volume policy label does not match")


def _validate_no_new_privileges(value: Any) -> None:
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError("egress proxy requires one no-new-privileges option")
    normalized = value[0].removesuffix(":true")
    if normalized != "no-new-privileges":
        raise ValueError("egress proxy no-new-privileges option does not match")


def _validate_proxy_healthcheck(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("egress proxy healthcheck configuration is missing")
    normalized = dict(value)
    # Newer Docker daemons may materialize the unset start-interval as zero.
    if normalized.pop("StartInterval", 0) != 0:
        raise ValueError("egress proxy healthcheck start interval does not match")
    if normalized != EGRESS_PROXY_HEALTHCHECK:
        raise ValueError("egress proxy healthcheck contract does not match")


def _validate_proxy_mounts(
    mounts: Any,
    *,
    expected_socket_volume: str,
) -> None:
    if not isinstance(mounts, list) or len(mounts) != 1:
        raise ValueError("egress proxy must have exactly one persistent mount")
    mount = mounts[0]
    if not isinstance(mount, dict):
        raise ValueError("egress proxy socket mount is malformed")
    if (
        mount.get("Type") != "volume"
        or mount.get("Name") != expected_socket_volume
        or mount.get("Destination") != EGRESS_SOCKET_DIR
        or mount.get("RW") is not True
    ):
        raise ValueError("egress proxy socket volume mount does not match")


def validate_proxy_container_inspect(
    payload: Any,
    *,
    expected_policy_sha256: str,
    expected_proxy_image_id: str,
    expected_socket_volume: str,
) -> tuple[str, str]:
    """Validate the effective running proxy, not only its image labels."""
    container = _single_inspect(payload, label="container")
    state = container.get("State")
    config = container.get("Config")
    host_config = container.get("HostConfig")
    network_settings = container.get("NetworkSettings")
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise ValueError("attested egress proxy container is not running")
    health = state.get("Health")
    if not isinstance(health, dict) or health.get("Status") != "healthy":
        raise ValueError("attested egress proxy container is not healthy")
    if container.get("Image") != expected_proxy_image_id:
        raise ValueError("running egress proxy image does not match evaluation")
    if not isinstance(config, dict):
        raise ValueError("running egress proxy configuration is missing")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise ValueError("running egress proxy has no attestation labels")
    if labels.get(EGRESS_ROLE_LABEL) != EGRESS_PROXY_ROLE:
        raise ValueError("running egress proxy role label does not match")
    if labels.get(EGRESS_POLICY_LABEL) != expected_policy_sha256:
        raise ValueError("running egress proxy policy label does not match")
    if labels.get(EGRESS_SOCKET_VOLUME_LABEL) != expected_socket_volume:
        raise ValueError("running egress proxy socket-volume label does not match")
    if tuple(config.get("Entrypoint") or ()) != EGRESS_PROXY_ENTRYPOINT:
        raise ValueError("running egress proxy entrypoint does not match")
    if not _empty(config.get("Cmd")):
        raise ValueError("running egress proxy command override is not allowed")
    if config.get("User") != EGRESS_PROXY_USER:
        raise ValueError("running egress proxy user does not match")
    _validate_proxy_healthcheck(config.get("Healthcheck"))

    if not isinstance(host_config, dict):
        raise ValueError("running egress proxy host configuration is missing")
    if host_config.get("ReadonlyRootfs") is not True:
        raise ValueError("egress proxy root filesystem must be read-only")
    if host_config.get("Privileged") is not False:
        raise ValueError("egress proxy must not be privileged")
    if not _empty(host_config.get("CapAdd")):
        raise ValueError("egress proxy must not add Linux capabilities")
    if host_config.get("CapDrop") != ["ALL"]:
        raise ValueError("egress proxy must drop every Linux capability")
    _validate_no_new_privileges(host_config.get("SecurityOpt"))
    if not _empty(host_config.get("Binds")):
        raise ValueError("egress proxy must not have bind mounts")
    if not _empty(host_config.get("PortBindings")):
        raise ValueError("egress proxy must not publish host ports")
    if host_config.get("PublishAllPorts") is not False:
        raise ValueError("egress proxy must not publish exposed ports")
    if host_config.get("Tmpfs") != EGRESS_PROXY_TMPFS:
        raise ValueError("egress proxy tmpfs mounts do not match")
    restart = host_config.get("RestartPolicy")
    if not isinstance(restart, dict) or restart.get("Name") != "unless-stopped":
        raise ValueError("egress proxy restart policy does not match")
    _validate_proxy_mounts(
        container.get("Mounts"),
        expected_socket_volume=expected_socket_volume,
    )

    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings, dict)
        else None
    )
    if not isinstance(networks, dict) or len(networks) != 1:
        raise ValueError("egress proxy must attach to exactly one public uplink")
    if not _empty(network_settings.get("Ports")):
        exposed = network_settings.get("Ports")
        if exposed != {"3128/tcp": None}:
            raise ValueError("egress proxy has unexpected published port state")
    network_name = next(iter(networks))
    if host_config.get("NetworkMode") != network_name:
        raise ValueError("egress proxy effective network mode does not match its uplink")
    container_id = container.get("Id")
    if not isinstance(container_id, str) or not container_id:
        raise ValueError("egress proxy container identity is missing")
    return container_id, network_name


def validate_proxy_image_inspect(
    payload: Any,
    *,
    expected_proxy_image_id: str,
    expected_policy_sha256: str,
) -> None:
    image = _single_inspect(payload, label="image")
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if image.get("Id") != expected_proxy_image_id:
        raise ValueError("egress proxy image inspection returned another image")
    if not isinstance(config, dict) or not isinstance(labels, dict):
        raise ValueError("egress proxy image configuration is missing")
    if labels.get(EGRESS_ROLE_LABEL) != EGRESS_PROXY_ROLE:
        raise ValueError("egress proxy image role label does not match")
    if labels.get(EGRESS_POLICY_LABEL) != expected_policy_sha256:
        raise ValueError("egress proxy image policy label does not match")
    if tuple(config.get("Entrypoint") or ()) != EGRESS_PROXY_ENTRYPOINT:
        raise ValueError("egress proxy image entrypoint does not match")
    if not _empty(config.get("Cmd")):
        raise ValueError("egress proxy image command must be empty")
    if config.get("User") != EGRESS_PROXY_USER:
        raise ValueError("egress proxy image user does not match")
    _validate_proxy_healthcheck(config.get("Healthcheck"))


def validate_probe_image_inspect(
    payload: Any,
    *,
    expected_probe_image_id: str,
) -> None:
    image = _single_inspect(payload, label="probe image")
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if image.get("Id") != expected_probe_image_id:
        raise ValueError("egress probe image inspection returned another image")
    if not isinstance(labels, dict):
        raise ValueError("egress probe image has no identity labels")
    if labels.get("dev.apdl.codegen.role") != "evaluation-controller":
        raise ValueError("egress probe must use the sealed evaluation controller")


def validate_public_uplink_inspect(payload: Any, *, expected_name: str) -> None:
    network = _single_inspect(payload, label="uplink network")
    if network.get("Name") != expected_name or network.get("Internal") is not False:
        raise ValueError("egress proxy uplink must be a non-internal Docker network")


def _docker_command(
    docker_bin: str,
    args: list[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    try:
        completed = runner(
            [docker_bin, *args],
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Codegen Docker egress attestation failed") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "")[-1000:]
        raise RuntimeError(f"Codegen Docker egress attestation failed: {detail}")
    if len(completed.stdout.encode("utf-8")) > _MAX_DOCKER_OUTPUT:
        raise RuntimeError("Codegen Docker egress attestation output exceeded its limit")
    return completed.stdout.strip()


def docker_control_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if source is None else source
    environment = {"PATH": source.get("PATH", os.defpath)}
    for key in ("HOME", "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        if key in source:
            environment[key] = source[key]
    return environment


def attest_docker_egress_policy(
    *,
    docker_bin: str,
    probe_image: str,
    launch_id: str,
    socket_volume: str,
    expected_policy_sha256: str,
    expected_proxy_image_id: str,
    proxy_url: str = EGRESS_PROXY_URL,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> EgressPolicyAttestation:
    """Attest the proxy and prove policy from the sealed controller image."""
    policy_sha256 = validate_policy_sha256(expected_policy_sha256)
    if (
        not launch_id
        or launch_id != launch_id.strip()
        or len(launch_id) > 200
        or "\x00" in launch_id
    ):
        raise ValueError("Codegen egress attestation requires a canonical launch id")
    proxy_image_id = validate_proxy_image_id(expected_proxy_image_id)
    probe_image_id = validate_proxy_image_id(probe_image)
    socket_volume_name = validate_socket_volume(socket_volume)
    canonical_proxy_url = validate_proxy_url(proxy_url)
    run = runner or subprocess.run
    docker_env = docker_control_environment(environment)

    volume_payload = _strict_json(
        _docker_command(
            docker_bin,
            ["volume", "inspect", socket_volume_name],
            environment=docker_env,
            timeout=20,
            runner=run,
        )
    )
    validate_socket_volume_inspect(
        volume_payload,
        expected_name=socket_volume_name,
        expected_policy_sha256=policy_sha256,
    )
    proxy_id = select_proxy_container_id(
        _docker_command(
            docker_bin,
            [
                "ps",
                "--quiet",
                "--no-trunc",
                "--filter",
                "status=running",
                "--filter",
                f"label={EGRESS_ROLE_LABEL}={EGRESS_PROXY_ROLE}",
                "--filter",
                f"label={EGRESS_POLICY_LABEL}={policy_sha256}",
                "--filter",
                f"label={EGRESS_SOCKET_VOLUME_LABEL}={socket_volume_name}",
            ],
            environment=docker_env,
            timeout=20,
            runner=run,
        )
    )
    container_payload = _strict_json(
        _docker_command(
            docker_bin,
            ["container", "inspect", proxy_id],
            environment=docker_env,
            timeout=20,
            runner=run,
        )
    )
    proxy_container_id, uplink = validate_proxy_container_inspect(
        container_payload,
        expected_policy_sha256=policy_sha256,
        expected_proxy_image_id=proxy_image_id,
        expected_socket_volume=socket_volume_name,
    )
    volume_consumers = _docker_command(
        docker_bin,
        [
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "status=running",
            "--filter",
            f"volume={socket_volume_name}",
        ],
        environment=docker_env,
        timeout=20,
        runner=run,
    )
    validate_socket_volume_exclusivity(
        volume_consumers,
        expected_proxy_container_id=proxy_container_id,
    )
    proxy_image_payload = _strict_json(
        _docker_command(
            docker_bin,
            ["image", "inspect", proxy_image_id],
            environment=docker_env,
            timeout=20,
            runner=run,
        )
    )
    validate_proxy_image_inspect(
        proxy_image_payload,
        expected_proxy_image_id=proxy_image_id,
        expected_policy_sha256=policy_sha256,
    )
    probe_image_payload = _strict_json(
        _docker_command(
            docker_bin,
            ["image", "inspect", probe_image_id],
            environment=docker_env,
            timeout=20,
            runner=run,
        )
    )
    validate_probe_image_inspect(
        probe_image_payload,
        expected_probe_image_id=probe_image_id,
    )
    uplink_payload = _strict_json(
        _docker_command(
            docker_bin,
            ["network", "inspect", uplink],
            environment=docker_env,
            timeout=20,
            runner=run,
        )
    )
    validate_public_uplink_inspect(uplink_payload, expected_name=uplink)

    probe_args = [
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
        "64",
        "--memory",
        "128m",
        "--cpus",
        "0.25",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
        "--mount",
        worker_socket_mount(socket_volume_name),
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    for name, value in proxy_environment(canonical_proxy_url).items():
        probe_args += ["-e", f"{name}={value}"]
    probe_args += [
        "--entrypoint",
        "python",
        probe_image_id,
        "-m",
        "app.egress",
        "probe",
        "--policy-sha256",
        policy_sha256,
        "--proxy-url",
        canonical_proxy_url,
        "--socket-path",
        EGRESS_SOCKET_PATH,
    ]
    probe_output = _docker_command(
        docker_bin,
        probe_args,
        environment=docker_env,
        timeout=30,
        runner=run,
    )
    probe = EgressProbeResult.model_validate(_strict_json(probe_output))
    if probe.policy_sha256 != policy_sha256:
        raise ValueError("egress deny probe policy identity does not match")
    post_probe_volume_consumers = _docker_command(
        docker_bin,
        [
            "ps",
            "--quiet",
            "--no-trunc",
            "--filter",
            "status=running",
            "--filter",
            f"volume={socket_volume_name}",
        ],
        environment=docker_env,
        timeout=20,
        runner=run,
    )
    validate_socket_volume_exclusivity(
        post_probe_volume_consumers,
        expected_proxy_container_id=proxy_container_id,
    )
    return EgressPolicyAttestation(
        launch_id=launch_id,
        policy_sha256=policy_sha256,
        socket_volume=socket_volume_name,
        proxy_container_id=proxy_container_id,
        proxy_image_id=proxy_image_id,
        probe_image_id=probe_image_id,
        proxy_url=canonical_proxy_url,
        probe=probe,
    )


def _proxy_request_status(proxy_url: str, request: bytes) -> int:
    parsed = urlsplit(proxy_url)
    assert parsed.hostname is not None and parsed.port is not None
    with socket.create_connection(
        (parsed.hostname, parsed.port),
        timeout=3,
    ) as connection:
        connection.sendall(request)
        response = bytearray()
        while b"\r\n" not in response and len(response) < 4096:
            chunk = connection.recv(512)
            if not chunk:
                break
            response.extend(chunk)
    first_line = bytes(response).split(b"\r\n", 1)[0]
    parts = first_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError(f"egress proxy returned a malformed response: {first_line!r}")
    return int(parts[1])


def _proxy_connect_status(proxy_url: str, target: str) -> int:
    request = (
        f"CONNECT {target} HTTP/1.1\r\n"
        f"Host: {target}\r\n"
        "Proxy-Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return _proxy_request_status(proxy_url, request)


def _proxy_http_status(proxy_url: str, target_url: str) -> int:
    parsed = urlsplit(target_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.port not in {None, 80}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("egress HTTP probe target must be an absolute port-80 URL")
    host = parsed.hostname
    request = (
        f"GET {target_url} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return _proxy_request_status(proxy_url, request)


def _require_network_unreachable(target: tuple[str, int], *, label: str) -> None:
    try:
        with socket.create_connection(target, timeout=2):
            pass
    except (ConnectionRefusedError, ConnectionResetError) as exc:
        raise RuntimeError(
            f"{label} remained reachable; refusal/reset is not isolation proof"
        ) from exc
    except TimeoutError:
        return
    except OSError as exc:
        if exc.errno in _EXPECTED_DIRECT_BLOCK_ERRNOS:
            return
        raise RuntimeError(
            f"{label} failed ambiguously instead of proving network isolation"
        ) from exc
    raise RuntimeError(f"{label} was reachable from a network-none worker")


def run_worker_egress_probe(
    *,
    policy_sha256: str,
    proxy_url: str,
) -> EgressProbeResult:
    policy_sha256 = validate_policy_sha256(policy_sha256)
    canonical_proxy_url = validate_proxy_url(proxy_url)
    if inherited_proxy_environment() != proxy_environment(canonical_proxy_url):
        raise RuntimeError("worker proxy environment is not canonical")
    proxy_http_denials = {
        target: _proxy_http_status(canonical_proxy_url, target)
        for target in _DENIED_PROXY_HTTP_TARGETS
    }
    if any(status != 403 for status in proxy_http_denials.values()):
        raise RuntimeError(
            "egress proxy did not deny every absolute-form metadata/private URL"
        )
    proxy_connect_denials = {
        target: _proxy_connect_status(canonical_proxy_url, target)
        for target in _DENIED_PROXY_CONNECT_TARGETS
    }
    if any(status != 403 for status in proxy_connect_denials.values()):
        raise RuntimeError(
            "egress proxy did not deny every metadata/private CONNECT target"
        )
    allowed_connect_status = _proxy_connect_status(
        canonical_proxy_url,
        _ALLOWED_PROXY_CONNECT_TARGET,
    )
    if allowed_connect_status != 200:
        raise RuntimeError(
            "egress proxy did not allow the canonical public control target"
        )
    _require_network_unreachable(_DIRECT_PUBLIC_TARGET, label="direct public egress")
    _require_network_unreachable(_DIRECT_PRIVATE_TARGET, label="direct private egress")
    _require_network_unreachable(_DIRECT_DNS_TARGET, label="direct external DNS")
    return EgressProbeResult(
        policy_sha256=policy_sha256,
        proxy_http_denials=proxy_http_denials,
        proxy_connect_denials=proxy_connect_denials,
        allowed_connect_status=allowed_connect_status,
        direct_public_blocked=True,
        direct_private_blocked=True,
        direct_dns_blocked=True,
    )


class UnixSocketProxyRelay(AbstractContextManager["UnixSocketProxyRelay"]):
    """Small sealed loopback TCP-to-Unix relay used inside network-none workers."""

    def __init__(
        self,
        *,
        socket_path: str,
        listen_host: str = EGRESS_PROXY_HOST,
        listen_port: int = EGRESS_PROXY_PORT,
    ) -> None:
        if listen_host != EGRESS_PROXY_HOST or listen_port != EGRESS_PROXY_PORT:
            raise ValueError("Codegen egress relay must use the canonical loopback port")
        if socket_path != EGRESS_SOCKET_PATH:
            raise ValueError("Codegen egress relay must use the canonical Unix socket")
        self._socket_path = socket_path
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._listener: socket.socket | None = None
        self._stopping = threading.Event()
        self._accept_thread: threading.Thread | None = None

    def __enter__(self) -> UnixSocketProxyRelay:
        try:
            mode = os.lstat(self._socket_path).st_mode
        except OSError as exc:
            raise RuntimeError("attested egress proxy socket is unavailable") from exc
        if not stat.S_ISSOCK(mode):
            raise RuntimeError("attested egress proxy path is not a Unix socket")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._listen_host, self._listen_port))
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept,
            name="codegen-egress-relay",
            daemon=True,
        )
        self._accept_thread.start()
        return self

    def __exit__(self, *_exc_info) -> None:
        self._stopping.set()
        if self._listener is not None:
            self._listener.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)

    def _accept(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                client, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                raise
            threading.Thread(
                target=self._forward,
                args=(client,),
                name="codegen-egress-connection",
                daemon=True,
            ).start()

    def _forward(self, client: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.settimeout(3)
            upstream.connect(self._socket_path)
            upstream.settimeout(None)
            client.settimeout(None)

            def pump(source: socket.socket, destination: socket.socket) -> None:
                try:
                    while not self._stopping.is_set():
                        chunk = source.recv(64 * 1024)
                        if not chunk:
                            break
                        destination.sendall(chunk)
                except OSError:
                    pass
                finally:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            upstream_to_client = threading.Thread(
                target=pump,
                args=(upstream, client),
                name="codegen-egress-upstream",
                daemon=True,
            )
            upstream_to_client.start()
            pump(client, upstream)
            upstream_to_client.join()
        except OSError:
            return
        finally:
            client.close()
            upstream.close()


def _relay_exec(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ValueError("relay-exec requires a command after --")
    with UnixSocketProxyRelay(
        socket_path=args.socket_path,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
    ):
        child = subprocess.Popen(command)
        previous_handlers: dict[int, Any] = {}

        def forward(signum: int, _frame: FrameType | None) -> None:
            if child.poll() is None:
                child.send_signal(signum)

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, forward)
        try:
            return child.wait()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            if child.poll() is None:
                child.kill()
                child.wait()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command_name", required=True)
    probe = subcommands.add_parser("probe")
    probe.add_argument("--policy-sha256", required=True)
    probe.add_argument("--proxy-url", required=True)
    probe.add_argument("--socket-path", required=True)
    relay_exec = subcommands.add_parser("relay-exec")
    relay_exec.add_argument("--socket-path", required=True)
    relay_exec.add_argument("--listen-host", required=True)
    relay_exec.add_argument("--listen-port", required=True, type=int)
    relay_exec.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command_name == "probe":
        with UnixSocketProxyRelay(socket_path=args.socket_path):
            result = run_worker_egress_probe(
                policy_sha256=args.policy_sha256,
                proxy_url=args.proxy_url,
            )
        print(result.model_dump_json())
        return
    if args.command_name == "relay-exec":
        raise SystemExit(_relay_exec(args))
    raise AssertionError("unreachable")


if __name__ == "__main__":
    main()
