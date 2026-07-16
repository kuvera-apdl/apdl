"""Strict contracts for network-none workers and the attested socket proxy."""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.egress import (
    EGRESS_POLICY_LABEL,
    EGRESS_PROXY_ENTRYPOINT,
    EGRESS_PROXY_HEALTHCHECK,
    EGRESS_PROXY_ROLE,
    EGRESS_PROXY_TMPFS,
    EGRESS_ROLE_LABEL,
    EGRESS_SOCKET_DIR,
    EGRESS_SOCKET_ROLE,
    EGRESS_SOCKET_VOLUME_LABEL,
    EgressProbeResult,
    UnixSocketProxyRelay,
    attest_docker_egress_policy,
    inherited_proxy_environment,
    proxy_environment,
    run_worker_egress_probe,
    validate_proxy_container_inspect,
    validate_socket_volume_exclusivity,
    validate_socket_volume_inspect,
)


POLICY = "a" * 64
PROBE_IMAGE = "sha256:" + "b" * 64
PROXY_IMAGE = "sha256:" + "c" * 64
PROXY_ID = "d" * 64
SOCKET_VOLUME = "apdl-codegen-test-egress"
UPLINK = "evaluation_default"


def _volume(*, policy: str = POLICY) -> list[dict]:
    return [
        {
            "Name": SOCKET_VOLUME,
            "Driver": "local",
            "Scope": "local",
            "Options": None,
            "Labels": {
                EGRESS_ROLE_LABEL: EGRESS_SOCKET_ROLE,
                EGRESS_POLICY_LABEL: policy,
            },
        }
    ]


def _proxy_container(
    *,
    policy: str = POLICY,
    privileged: bool = False,
    mounts: list[dict] | None = None,
) -> list[dict]:
    return [
        {
            "Id": PROXY_ID,
            "Image": PROXY_IMAGE,
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "Config": {
                "Labels": {
                    EGRESS_POLICY_LABEL: policy,
                    EGRESS_ROLE_LABEL: EGRESS_PROXY_ROLE,
                    EGRESS_SOCKET_VOLUME_LABEL: SOCKET_VOLUME,
                },
                "Entrypoint": list(EGRESS_PROXY_ENTRYPOINT),
                "Cmd": [],
                "User": "proxy",
                "Healthcheck": dict(EGRESS_PROXY_HEALTHCHECK),
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": privileged,
                "CapAdd": None,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Binds": None,
                "PortBindings": {},
                "PublishAllPorts": False,
                "Tmpfs": EGRESS_PROXY_TMPFS,
                "RestartPolicy": {"Name": "unless-stopped"},
                "NetworkMode": UPLINK,
            },
            "Mounts": mounts
            or [
                {
                    "Type": "volume",
                    "Name": SOCKET_VOLUME,
                    "Destination": EGRESS_SOCKET_DIR,
                    "RW": True,
                }
            ],
            "NetworkSettings": {
                "Networks": {UPLINK: {}},
                "Ports": {"3128/tcp": None},
            },
        }
    ]


def _proxy_image() -> list[dict]:
    return [
        {
            "Id": PROXY_IMAGE,
            "Config": {
                "Labels": {
                    EGRESS_POLICY_LABEL: POLICY,
                    EGRESS_ROLE_LABEL: EGRESS_PROXY_ROLE,
                },
                "Entrypoint": list(EGRESS_PROXY_ENTRYPOINT),
                "Cmd": [],
                "User": "proxy",
                "Healthcheck": dict(EGRESS_PROXY_HEALTHCHECK),
            },
        }
    ]


def _probe_image() -> list[dict]:
    return [
        {
            "Id": PROBE_IMAGE,
            "Config": {
                "Labels": {"dev.apdl.codegen.role": "evaluation-controller"}
            },
        }
    ]


def _probe() -> dict:
    return {
        "schema_version": "codegen_egress_probe@3",
        "transport": "network_none_unix_socket@1",
        "policy_sha256": POLICY,
        "proxy_http_denials": {
            "http://169.254.169.254/latest/meta-data/": 403,
            "http://10.0.0.1/": 403,
            "http://172.16.0.1/": 403,
            "http://192.168.0.1/": 403,
        },
        "proxy_connect_denials": {
            "169.254.169.254:443": 403,
            "10.0.0.1:443": 403,
            "172.16.0.1:443": 403,
            "192.168.0.1:443": 403,
        },
        "allowed_connect_status": 200,
        "direct_public_blocked": True,
        "direct_private_blocked": True,
        "direct_dns_blocked": True,
    }


def test_proxy_environment_is_complete_and_loopback_only():
    expected = proxy_environment()

    assert expected["HTTP_PROXY"] == "http://127.0.0.1:3128"
    assert inherited_proxy_environment(expected) == expected
    with pytest.raises(ValueError, match="incomplete or non-canonical"):
        inherited_proxy_environment({"HTTP_PROXY": expected["HTTP_PROXY"]})


def test_socket_volume_requires_exact_local_labels():
    validate_socket_volume_inspect(
        _volume(),
        expected_name=SOCKET_VOLUME,
        expected_policy_sha256=POLICY,
    )
    with pytest.raises(ValueError, match="policy label"):
        validate_socket_volume_inspect(
            _volume(policy="f" * 64),
            expected_name=SOCKET_VOLUME,
            expected_policy_sha256=POLICY,
        )


def test_proxy_contract_attests_effective_security_and_exact_mounts():
    container_id, uplink = validate_proxy_container_inspect(
        _proxy_container(),
        expected_policy_sha256=POLICY,
        expected_proxy_image_id=PROXY_IMAGE,
        expected_socket_volume=SOCKET_VOLUME,
    )
    assert container_id == PROXY_ID
    assert uplink == UPLINK

    with pytest.raises(ValueError, match="must not be privileged"):
        validate_proxy_container_inspect(
            _proxy_container(privileged=True),
            expected_policy_sha256=POLICY,
            expected_proxy_image_id=PROXY_IMAGE,
            expected_socket_volume=SOCKET_VOLUME,
        )
    healthcheck_override = _proxy_container()
    healthcheck_override[0]["Config"]["Healthcheck"]["Retries"] = 1
    with pytest.raises(ValueError, match="healthcheck contract"):
        validate_proxy_container_inspect(
            healthcheck_override,
            expected_policy_sha256=POLICY,
            expected_proxy_image_id=PROXY_IMAGE,
            expected_socket_volume=SOCKET_VOLUME,
        )
    unexpected_mount = {
        "Type": "bind",
        "Source": "/host",
        "Destination": "/host",
        "RW": False,
    }
    with pytest.raises(ValueError, match="exactly one persistent mount"):
        validate_proxy_container_inspect(
            _proxy_container(
                mounts=[*_proxy_container()[0]["Mounts"], unexpected_mount]
            ),
            expected_policy_sha256=POLICY,
            expected_proxy_image_id=PROXY_IMAGE,
            expected_socket_volume=SOCKET_VOLUME,
        )


def test_socket_volume_exclusivity_requires_only_the_full_proxy_id():
    validate_socket_volume_exclusivity(
        PROXY_ID,
        expected_proxy_container_id=PROXY_ID,
    )
    with pytest.raises(ValueError, match="mounted only"):
        validate_socket_volume_exclusivity(
            PROXY_ID[:12],
            expected_proxy_container_id=PROXY_ID,
        )
    with pytest.raises(ValueError, match="mounted only"):
        validate_socket_volume_exclusivity(
            f"{PROXY_ID}\n{'e' * 64}",
            expected_proxy_container_id=PROXY_ID,
        )


def test_full_attestation_uses_controller_probe_on_network_none():
    commands: list[list[str]] = []
    volume_checks = 0

    def runner(argv, **_kwargs):
        nonlocal volume_checks
        commands.append(argv)
        args = argv[1:]
        if args[:3] == ["volume", "inspect", SOCKET_VOLUME]:
            output = json.dumps(_volume())
        elif args[:3] == ["ps", "--quiet", "--no-trunc"]:
            if f"volume={SOCKET_VOLUME}" in args:
                volume_checks += 1
            output = PROXY_ID
        elif args[:3] == ["container", "inspect", PROXY_ID]:
            output = json.dumps(_proxy_container())
        elif args[:3] == ["image", "inspect", PROXY_IMAGE]:
            output = json.dumps(_proxy_image())
        elif args[:3] == ["image", "inspect", PROBE_IMAGE]:
            output = json.dumps(_probe_image())
        elif args[:3] == ["network", "inspect", UPLINK]:
            output = json.dumps([{"Name": UPLINK, "Internal": False}])
        elif args[0] == "run":
            output = json.dumps(_probe())
        else:
            raise AssertionError(f"unexpected Docker command: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    attestation = attest_docker_egress_policy(
        docker_bin="docker",
        probe_image=PROBE_IMAGE,
        launch_id="eval_inv_" + "e" * 32,
        socket_volume=SOCKET_VOLUME,
        expected_policy_sha256=POLICY,
        expected_proxy_image_id=PROXY_IMAGE,
        environment={"PATH": "/usr/bin"},
        runner=runner,
    )

    assert attestation.launch_id == "eval_inv_" + "e" * 32
    assert attestation.probe_image_id == PROBE_IMAGE
    assert len(attestation.evidence_sha256()) == 64
    probe_command = next(command for command in commands if command[1] == "run")
    assert probe_command[probe_command.index("--network") + 1] == "none"
    mount = probe_command[probe_command.index("--mount") + 1]
    assert f"src={SOCKET_VOLUME}" in mount
    assert "readonly" in mount
    assert PROBE_IMAGE in probe_command
    assert PROXY_IMAGE not in probe_command
    assert volume_checks == 2
    ps_commands = [command for command in commands if command[1] == "ps"]
    assert ps_commands
    assert all("--no-trunc" in command for command in ps_commands)


def test_attestation_rejects_a_new_volume_consumer_after_the_probe():
    volume_checks = 0

    def runner(argv, **_kwargs):
        nonlocal volume_checks
        args = argv[1:]
        if args[:3] == ["volume", "inspect", SOCKET_VOLUME]:
            output = json.dumps(_volume())
        elif args[:3] == ["ps", "--quiet", "--no-trunc"]:
            if f"volume={SOCKET_VOLUME}" in args:
                volume_checks += 1
                output = (
                    PROXY_ID
                    if volume_checks == 1
                    else f"{PROXY_ID}\n{'e' * 64}"
                )
            else:
                output = PROXY_ID
        elif args[:3] == ["container", "inspect", PROXY_ID]:
            output = json.dumps(_proxy_container())
        elif args[:3] == ["image", "inspect", PROXY_IMAGE]:
            output = json.dumps(_proxy_image())
        elif args[:3] == ["image", "inspect", PROBE_IMAGE]:
            output = json.dumps(_probe_image())
        elif args[:3] == ["network", "inspect", UPLINK]:
            output = json.dumps([{"Name": UPLINK, "Internal": False}])
        elif args[0] == "run":
            output = json.dumps(_probe())
        else:
            raise AssertionError(f"unexpected Docker command: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")

    with pytest.raises(ValueError, match="mounted only"):
        attest_docker_egress_policy(
            docker_bin="docker",
            probe_image=PROBE_IMAGE,
            launch_id="eval_inv_" + "e" * 32,
            socket_volume=SOCKET_VOLUME,
            expected_policy_sha256=POLICY,
            expected_proxy_image_id=PROXY_IMAGE,
            environment={"PATH": "/usr/bin"},
            runner=runner,
        )

    assert volume_checks == 2


def test_probe_schema_cannot_claim_a_successful_bypass_as_blocked():
    payload = _probe()
    payload["direct_public_blocked"] = False

    with pytest.raises(ValidationError):
        EgressProbeResult.model_validate(payload)


def test_probe_schema_rejects_deny_all_and_connect_only_evidence():
    deny_all = _probe()
    deny_all["allowed_connect_status"] = 403
    with pytest.raises(ValidationError):
        EgressProbeResult.model_validate(deny_all)

    connect_only = _probe()
    del connect_only["proxy_http_denials"]
    with pytest.raises(ValidationError):
        EgressProbeResult.model_validate(connect_only)


def test_worker_probe_accepts_only_unreachable_or_timeout(monkeypatch):
    for name, value in proxy_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("app.egress._proxy_http_status", lambda *_args: 403)

    def connect_status(_proxy_url, target):
        return 200 if target == "api.github.com:443" else 403

    monkeypatch.setattr("app.egress._proxy_connect_status", connect_status)
    direct_targets: list[tuple[str, int]] = []

    def unreachable(target, **_kwargs):
        direct_targets.append(target)
        raise OSError(errno.ENETUNREACH, "network unreachable")

    monkeypatch.setattr("app.egress.socket.create_connection", unreachable)
    result = run_worker_egress_probe(
        policy_sha256=POLICY,
        proxy_url="http://127.0.0.1:3128",
    )

    assert result.direct_public_blocked is True
    assert result.direct_private_blocked is True
    assert result.direct_dns_blocked is True
    assert direct_targets == [
        ("1.1.1.1", 443),
        ("169.254.169.254", 80),
        ("8.8.8.8", 53),
    ]


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
        ConnectionResetError(errno.ECONNRESET, "reset"),
    ],
)
def test_worker_probe_rejects_refusal_or_reset_as_isolation(monkeypatch, error):
    for name, value in proxy_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("app.egress._proxy_http_status", lambda *_args: 403)

    def connect_status(_proxy_url, target):
        return 200 if target == "api.github.com:443" else 403

    monkeypatch.setattr("app.egress._proxy_connect_status", connect_status)
    monkeypatch.setattr(
        "app.egress.socket.create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(RuntimeError, match="not isolation proof"):
        run_worker_egress_probe(
            policy_sha256=POLICY,
            proxy_url="http://127.0.0.1:3128",
        )


def test_worker_probe_rejects_connect_only_policy(monkeypatch):
    for name, value in proxy_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("app.egress._proxy_http_status", lambda *_args: 200)
    monkeypatch.setattr("app.egress._proxy_connect_status", lambda *_args: 403)

    with pytest.raises(RuntimeError, match="absolute-form"):
        run_worker_egress_probe(
            policy_sha256=POLICY,
            proxy_url="http://127.0.0.1:3128",
        )


def test_relay_rejects_symlinked_socket_path(tmp_path: Path, monkeypatch):
    target = tmp_path / "target"
    target.write_text("not a socket", encoding="utf-8")
    link = tmp_path / "proxy.sock"
    link.symlink_to(target)
    monkeypatch.setattr("app.egress.EGRESS_SOCKET_PATH", str(link))

    with pytest.raises(RuntimeError, match="not a Unix socket"):
        with UnixSocketProxyRelay(socket_path=str(link)):
            pass


def test_relay_preserves_large_payloads_in_both_directions(monkeypatch):
    unix_path = Path(f"/tmp/cg-egress-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
    port_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port_probe.bind(("127.0.0.1", 0))
    relay_port = port_probe.getsockname()[1]
    port_probe.close()
    monkeypatch.setattr("app.egress.EGRESS_SOCKET_PATH", str(unix_path))
    monkeypatch.setattr("app.egress.EGRESS_PROXY_PORT", relay_port)

    request = (b"request-" * (512 * 1024))[:3_000_000]
    response = (b"response-" * (512 * 1024))[:3_500_000]
    observed = bytearray()
    ready = threading.Event()

    def unix_server() -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(unix_path))
        server.listen(1)
        ready.set()
        connection, _ = server.accept()
        with connection:
            while True:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    break
                observed.extend(chunk)
            connection.sendall(response)
        server.close()

    server_thread = threading.Thread(target=unix_server, daemon=True)
    server_thread.start()
    assert ready.wait(timeout=2)

    with UnixSocketProxyRelay(
        socket_path=str(unix_path),
        listen_port=relay_port,
    ):
        with socket.create_connection(("127.0.0.1", relay_port), timeout=2) as client:
            client.settimeout(10)
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            received = bytearray()
            while True:
                chunk = client.recv(64 * 1024)
                if not chunk:
                    break
                received.extend(chunk)

    server_thread.join(timeout=2)
    assert not server_thread.is_alive()
    assert bytes(observed) == request
    assert bytes(received) == response
    unix_path.unlink(missing_ok=True)


def test_proxy_image_prepares_fresh_socket_volume_for_non_root_user():
    dockerfile = (
        Path(__file__).parents[3]
        / "infra/docker/codegen-egress/Dockerfile"
    ).read_text(encoding="utf-8")
    prepare = dockerfile.index("mkdir -p /run/apdl-codegen-egress")
    chown = dockerfile.index("/run/apdl-codegen-egress", prepare + 1)
    user = dockerfile.index("USER proxy")

    assert prepare < chown < user
