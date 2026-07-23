"""Tests for the sandboxed (container) editor's pure logic + never-raise contract.

The real path (an actual `docker run` of the sandbox image) is integration-
untested; these cover argv/env assembly, the secrets-off-argv property, result
parsing, and that an attempt never raises.
"""

import asyncio
import json
import subprocess

import pytest

from app.contracts.models import ContractBundle
from app.editor.base import EditRequest
from app.editor.container_editor import ContainerAiderEditor, _last_json
from app.editor.environment import MODEL_PROVIDER_ENV
from app.editor.worker_contract import (
    decode_codegen_worker_request,
    encode_codegen_worker_request,
)
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.inspection.preflight import RepositoryPreflightAttestation
from app.profiling import RepoProfile
from app.requirements import compile_requirement_ledger
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    RuntimeAcceptancePlan,
)
from app.safety.policy import (
    PlatformCodegenSafetyPolicy,
    TenantCodegenConnectionPolicy,
    TenantCodegenGatesPolicy,
    resolve_effective_policy,
)
from app.semantic_review import assemble_review_verdict
from app.verification import build_verification_plan, evaluate_verification_coverage


def _req(**over) -> EditRequest:
    base = dict(
        repo="acme/widgets", base_branch="main", branch="apdl/x",
        token="ghs_secrettoken", title="Add thing", spec="do the thing",
        constraints=["keep tests green"], test_cmd="python -m pytest -q",
        repository_preflight=RepositoryPreflightAttestation(
            repository="acme/widgets",
            source_branch="main",
            head_sha="a" * 40,
            tree_sha="b" * 40,
            file_count=3,
        ),
    )
    base.update(over)
    return EditRequest(**base)


def test_docker_argv_has_hardening_and_image_last():
    editor = ContainerAiderEditor(image="apdl-sandbox:test")
    argv = editor._docker_argv(_req())
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--cap-drop" in argv and "ALL" in argv
    assert "no-new-privileges" in argv
    assert "--read-only" in argv
    assert argv[argv.index("--pid") + 1] == "private"
    assert argv.count("--tmpfs") == 2
    assert "--user" in argv and "1000:1000" in argv
    assert "--pids-limit" in argv and "--memory" in argv and "--cpus" in argv
    assert argv[-1] == "apdl-sandbox:test"  # image is the final arg


def test_docker_argv_names_container_for_forced_cleanup():
    argv = ContainerAiderEditor()._docker_argv(
        _req(), container_name="apdl-codegen-test"
    )
    name_index = argv.index("--name")
    assert argv[name_index + 1] == "apdl-codegen-test"


def test_docker_argv_contains_no_task_request_values():
    request = _req(
        title="sentinel-title",
        spec="sentinel task secret",
        constraints=["sentinel-constraint"],
        test_cmd="sentinel-test-command",
    )
    editor = ContainerAiderEditor()
    argv = " ".join(editor._docker_argv(request))
    environment = "\0".join(editor._docker_env(request).values())
    for value in (
        "acme/widgets",
        "apdl/x",
        "sentinel-title",
        "sentinel task secret",
        "sentinel-constraint",
        "sentinel-test-command",
        "ghs_secrettoken",
    ):
        assert value not in argv
        assert value not in environment
    assert "CS_" not in argv
    assert "HOME=/workspace/home" in argv


def test_worker_envelope_carries_effective_safety_policy_and_revert_sha():
    safety_policy = resolve_effective_policy(
        TenantCodegenConnectionPolicy(
            gates=TenantCodegenGatesPolicy(max_files=5)
        ),
        PlatformCodegenSafetyPolicy(),
    )
    req = _req(safety_policy=safety_policy, revert_sha="cafebabe")
    envelope = decode_codegen_worker_request(
        encode_codegen_worker_request(req)
    )
    assert envelope.safety_policy == safety_policy
    assert envelope.safety_policy_sha256 == safety_policy.canonical_digest()
    assert envelope.revert_sha == "cafebabe"


def test_docker_argv_forwards_editor_config(monkeypatch):
    # The sandboxed AiderEditor must behave exactly like the in-process one:
    # operator knobs (fail-closed posture, timeouts, pass toggles) ride along.
    monkeypatch.setenv("CODEGEN_REQUIRE_VERIFY", "false")
    monkeypatch.setenv("CODEGEN_CONVENTIONS", "false")
    argv = " ".join(ContainerAiderEditor()._docker_argv(_req()))
    assert "CODEGEN_REQUIRE_VERIFY=false" in argv
    assert "CODEGEN_CONVENTIONS=false" in argv


def test_container_timeout_covers_the_full_job_budget(monkeypatch):
    # The container runs the whole worker pipeline, but its credential-bearing wall
    # time remains below GitHub's one-hour installation-token lifetime.
    monkeypatch.setenv("CODEGEN_TIMEOUT", "1800")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "300")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    assert ContainerAiderEditor()._timeout == 3000


def test_pr_runtime_preflight_accepts_exact_image_revision_and_socket_proxy(monkeypatch):
    revision = "evaluated-revision"
    image = "sha256:" + "a" * 64
    policy = "b" * 64
    proxy_image = "sha256:" + "c" * 64
    controller_image = "sha256:" + "d" * 64
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", policy)
    monkeypatch.setenv("CODEGEN_EGRESS_PROXY_IMAGE_ID", proxy_image)
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "codegen-egress-socket")
    monkeypatch.setenv("CODEGEN_CONTROLLER_IMAGE_ID", controller_image)
    calls: list[list[str]] = []
    responses = iter(["27.5.1", revision])
    attestation: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 20
        return subprocess.CompletedProcess(argv, 0, next(responses), "")

    monkeypatch.setattr("app.editor.container_editor.subprocess.run", fake_run)
    monkeypatch.setattr(
        "app.editor.container_editor.attest_docker_egress_policy",
        lambda **kwargs: attestation.update(kwargs) or object(),
    )

    ContainerAiderEditor(image=image).assert_runtime_ready(
        expected_revision=revision
    )

    assert calls == [
        ["docker", "version", "--format", "{{.Server.Version}}"],
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "dev.apdl.codegen.revision" }}',
            image,
        ],
    ]
    assert attestation["probe_image"] == controller_image
    assert attestation["launch_id"] == "codegen-runtime-startup"
    assert attestation["socket_volume"] == "codegen-egress-socket"
    assert attestation["expected_policy_sha256"] == policy
    assert attestation["expected_proxy_image_id"] == proxy_image


def test_development_runtime_preflight_accepts_revision_labeled_tag(monkeypatch):
    revision = "local-development"
    image = "apdl-codegen-sandbox:local-development"
    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "codegen-development")
    calls: list[list[str]] = []
    responses = iter(["27.5.1", revision, "[]"])

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, next(responses), "")

    monkeypatch.setattr("app.editor.container_editor.subprocess.run", fake_run)

    ContainerAiderEditor(image=image).assert_runtime_ready(
        expected_revision=revision,
        require_immutable_image=False,
        require_egress_policy=False,
    )

    assert calls == [
        ["docker", "version", "--format", "{{.Server.Version}}"],
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "dev.apdl.codegen.revision" }}',
            image,
        ],
        ["docker", "network", "inspect", "codegen-development"],
    ]


def test_attested_worker_uses_network_none_socket_configuration(monkeypatch):
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "b" * 64)
    monkeypatch.setenv(
        "CODEGEN_EGRESS_PROXY_IMAGE_ID",
        "sha256:" + "c" * 64,
    )
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "codegen-egress-socket")
    monkeypatch.setenv(
        "CODEGEN_CONTROLLER_IMAGE_ID",
        "sha256:" + "d" * 64,
    )

    argv = ContainerAiderEditor()._docker_argv(_req())

    assert argv[argv.index("--network") + 1] == "none"
    socket_mount = argv[argv.index("--mount") + 1]
    assert "src=codegen-egress-socket" in socket_mount
    assert "readonly" in socket_mount
    assert "relay-exec" in argv
    assert "HTTP_PROXY=http://127.0.0.1:3128" in argv
    assert "https_proxy=http://127.0.0.1:3128" in argv


@pytest.mark.parametrize("network", ["bridge", "default", "host", "none", "custom"])
def test_evaluated_runtime_rejects_any_configured_network(monkeypatch, network):
    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", network)
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "b" * 64)
    monkeypatch.setenv(
        "CODEGEN_EGRESS_PROXY_IMAGE_ID",
        "sha256:" + "c" * 64,
    )
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "codegen-egress-socket")
    monkeypatch.setenv(
        "CODEGEN_CONTROLLER_IMAGE_ID",
        "sha256:" + "d" * 64,
    )

    with pytest.raises(ValueError, match="--network none"):
        ContainerAiderEditor(image="sha256:" + "a" * 64)


def test_pr_runtime_preflight_rejects_mutable_candidate_image(monkeypatch):
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "b" * 64)
    monkeypatch.setenv(
        "CODEGEN_EGRESS_PROXY_IMAGE_ID",
        "sha256:" + "c" * 64,
    )
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "codegen-egress-socket")
    monkeypatch.setenv(
        "CODEGEN_CONTROLLER_IMAGE_ID",
        "sha256:" + "d" * 64,
    )

    with pytest.raises(RuntimeError, match="immutable sandbox image digest"):
        ContainerAiderEditor(image="apdl-codegen-sandbox:latest").assert_runtime_ready(
            expected_revision="evaluated-revision"
        )


def test_pr_runtime_preflight_rejects_mismatched_image_revision(monkeypatch):
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "b" * 64)
    monkeypatch.setenv(
        "CODEGEN_EGRESS_PROXY_IMAGE_ID",
        "sha256:" + "c" * 64,
    )
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "codegen-egress-socket")
    monkeypatch.setenv(
        "CODEGEN_CONTROLLER_IMAGE_ID",
        "sha256:" + "d" * 64,
    )
    responses = iter(["27.5.1", "different-revision"])

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, next(responses), "")

    monkeypatch.setattr("app.editor.container_editor.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="does not match CODEGEN_REVISION"):
        ContainerAiderEditor(image="sha256:" + "a" * 64).assert_runtime_ready(
            expected_revision="evaluated-revision"
        )


def test_secrets_are_passed_by_name_not_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    editor = ContainerAiderEditor()
    argv = editor._docker_argv(_req())
    joined = " ".join(argv)
    # The provider key NAME is forwarded; repository authority is not.
    assert "ANTHROPIC_API_KEY" in argv
    assert "GH_TOKEN" not in argv
    # ...but their VALUES never touch the argv.
    assert "ghs_secrettoken" not in joined
    assert "sk-ant-secretvalue" not in joined
    # The App private key is never forwarded at all.
    assert "GITHUB_APP_PRIVATE_KEY" not in argv


def test_docker_env_carries_provider_secrets_but_not_repository_or_internal(
    monkeypatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "internal")
    env = ContainerAiderEditor()._docker_env(_req())
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-secretvalue"
    assert "GH_TOKEN" not in env
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert "APDL_INTERNAL_TOKEN" not in env


def test_preflight_launch_has_private_pid_namespace_and_no_provider_env(
    monkeypatch,
):
    for name in MODEL_PROVIDER_ENV:
        monkeypatch.setenv(name, f"secret-{name}")
    editor = ContainerAiderEditor(image="apdl-sandbox:test")

    argv = editor._preflight_argv(
        _req(),
        container_name="apdl-codegen-inspect-test",
    )
    env = editor._docker_control_env()
    joined = " ".join(argv)

    assert argv[argv.index("--pid") + 1] == "private"
    assert "dev.apdl.codegen.role=inspection" in argv
    assert argv[-3:] == [
        "apdl-sandbox:test",
        "-m",
        "app.inspection.preflight_cli",
    ]
    assert "CS_REPO=acme/widgets" in argv
    assert "CS_SOURCE_BRANCH=main" in argv
    assert "GH_TOKEN" not in joined
    assert "ghs_secrettoken" not in joined
    for name in MODEL_PROVIDER_ENV:
        assert name not in argv
        assert f"secret-{name}" not in joined
        assert name not in env


def test_last_json_finds_result_among_noise():
    blob = "cloning...\nrunning aider\n" + json.dumps({"success": True, "branch": "b"})
    assert _last_json(blob) == {"success": True, "branch": "b"}
    assert _last_json("no json here\n{bad json}") is None


def test_parse_preflight_rejects_identity_substitution():
    editor = ContainerAiderEditor()
    payload = json.dumps(
        {
            "success": True,
            "attestation": {
                "schema_version": "repository_preflight@1",
                "repository": "other/widgets",
                "source_branch": "main",
                "head_sha": "a" * 40,
                "tree_sha": "b" * 40,
                "file_count": 1,
            },
        }
    )

    with pytest.raises(RuntimeError, match="identity mismatch"):
        editor._parse_preflight_result(0, payload, "", _req())


def test_parse_result_maps_success_json():
    editor = ContainerAiderEditor()
    ledger = compile_requirement_ledger(title="Add thing", spec="Do the thing.")
    plan = build_verification_plan(ledger, RepoProfile())
    coverage = evaluate_verification_coverage(plan, changed_paths=["a.py", "b.py"])
    review = assemble_review_verdict(
        ledger=ledger,
        contracts=ContractBundle(),
        dependency_slice=DependencySlice(),
        verification_plan=plan,
        verification_coverage=coverage,
        diff_text="diff --git a/a.py b/a.py\n+changed",
        model_response_text=None,
    )
    runtime_plan = RuntimeAcceptancePlan(
        source_ledger_sha256="a" * 64,
        repo_profile_sha256="b" * 64,
        verification_plan_sha256="c" * 64,
    )
    workflow = GeneratedRuntimeWorkflowAttestation(
        path=".github/workflows/apdl-runtime-acceptance.yml",
        content_sha256="d" * 64,
        runtime_acceptance_plan_sha256=runtime_plan.evidence_hash(),
    )
    out = json.dumps({
        "success": True, "branch": "apdl/x",
        "diff_stat": {"files": 2, "additions": 9, "deletions": 1},
        "changed_paths": ["a.py", "b.py"], "diff_text": "diff…", "error": None,
        "base_sha": "a" * 40,
        "candidate_tree_sha": "b" * 40,
        "patch_base64": "cGF0Y2g=",
        "prompts": [{"stage": "edit", "label": "one", "system": None, "user": "u", "notes": None}],
        "contract_bundle": ContractBundle().model_dump(mode="json"),
        "requirement_ledger": ledger.model_dump(mode="json"),
        "inspection_snapshot": InspectionSnapshot().model_dump(mode="json"),
        "dependency_slice": DependencySlice().model_dump(mode="json"),
        "verification_plan": plan.model_dump(mode="json"),
        "verification_coverage": coverage.model_dump(mode="json"),
        "runtime_acceptance_plan": runtime_plan.model_dump(mode="json"),
        "generated_runtime_workflow": workflow.model_dump(mode="json"),
        "review_verdict": review.model_dump(mode="json"),
    })
    res = editor._parse_result(0, out, "", _req())
    assert res.success is True
    assert res.diff_stat["files"] == 2
    assert res.changed_paths == ["a.py", "b.py"]
    assert res.base_sha == "a" * 40
    assert res.candidate_tree_sha == "b" * 40
    assert res.patch_base64 == "cGF0Y2g="
    assert res.prompts[0]["stage"] == "edit"
    assert res.contract_bundle == ContractBundle()
    assert res.requirement_ledger == ledger
    assert res.inspection_snapshot == InspectionSnapshot()
    assert res.dependency_slice == DependencySlice()
    assert res.verification_plan == plan
    assert res.verification_coverage == coverage
    assert res.runtime_acceptance_plan == runtime_plan
    assert res.generated_runtime_workflow == workflow
    assert res.review_verdict == review


def test_parse_result_no_json_is_failure_with_stderr_tail():
    res = ContainerAiderEditor()._parse_result(125, "", "docker: no such image", _req())
    assert res.success is False
    assert "docker: no such image" in (res.error or "")
    assert res.branch == "apdl/x"


def test_parse_result_tail_is_line_safe_and_marks_truncation():
    stderr = "header\n" + ("x" * 900) + "\ncomplete traceback line\nfinal cause"

    res = ContainerAiderEditor()._parse_result(1, "", stderr, _req())

    assert res.success is False
    error = res.error or ""
    assert "[…truncated " in error
    assert "complete traceback line\nfinal cause" in error
    assert ("x" * 20) not in error


def test_parse_result_tail_omits_a_single_overlong_line():
    res = ContainerAiderEditor()._parse_result(1, "", "x" * 1000, _req())

    assert res.success is False
    error = res.error or ""
    assert "final line exceeds the 800-char excerpt limit" in error
    assert "x" * 10 not in error


@pytest.mark.asyncio
async def test_implement_never_raises_on_docker_fault(monkeypatch):
    editor = ContainerAiderEditor()

    async def boom(*_a, **_k):
        raise RuntimeError("docker daemon unreachable")

    monkeypatch.setattr(editor, "_run_docker", boom)
    res = await editor.implement(_req())
    assert res.success is False
    assert "docker daemon unreachable" in (res.error or "")


@pytest.mark.asyncio
async def test_oversized_task_is_rejected_before_any_container_launch(monkeypatch):
    editor = ContainerAiderEditor()
    launched = False

    async def run(*_args, **_kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("oversized input must not reach Docker")

    monkeypatch.setattr(editor, "_run_docker", run)
    result = await editor.implement(_req(spec="x" * (256 * 1024 + 1)))

    assert result.success is False
    assert "strict schema" in (result.error or "")
    assert launched is False


@pytest.mark.asyncio
async def test_implement_reattests_egress_immediately_before_worker(monkeypatch):
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "b" * 64)
    monkeypatch.setenv(
        "CODEGEN_EGRESS_PROXY_IMAGE_ID",
        "sha256:" + "c" * 64,
    )
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "codegen-egress-socket")
    monkeypatch.setenv(
        "CODEGEN_CONTROLLER_IMAGE_ID",
        "sha256:" + "d" * 64,
    )
    editor = ContainerAiderEditor(image="sha256:" + "a" * 64)
    events: list[str] = []

    def attest(*, launch_id):
        if "inspect" in launch_id:
            events.append("attest-inspection")
        else:
            events.append("attest-editor")
        return object()

    async def run(_argv, _env, *, container_name, stdin_data):
        assert container_name.startswith("apdl-codegen-")
        if "app.inspection.preflight_cli" in _argv:
            assert json.loads(stdin_data) == {"read_token": "ghs_secrettoken"}
            events.append("preflight")
            return (
                0,
                json.dumps(
                    {
                        "success": True,
                        "attestation": _req().repository_preflight.model_dump(
                            mode="json"
                        ),
                    }
                ),
                "",
            )
        assert decode_codegen_worker_request(stdin_data).spec == "do the thing"
        events.append("editor")
        return 0, json.dumps({"success": True, "branch": "apdl/x"}), ""

    monkeypatch.setattr(editor, "_attest_egress_policy", attest)
    monkeypatch.setattr(editor, "_run_docker", run)

    result = await editor.implement(_req())

    assert result.success is True
    assert events == [
        "attest-inspection",
        "preflight",
        "attest-editor",
        "editor",
    ]


@pytest.mark.asyncio
async def test_outer_timeout_stops_named_credential_bearing_container(monkeypatch):
    editor = ContainerAiderEditor()
    editor._timeout = 0.001
    events: list[str] = []

    class HangingProcess:
        def __init__(self):
            self.returncode = None

        async def communicate(self):
            await asyncio.Event().wait()

        def terminate(self):
            events.append("terminate-run-client")
            self.returncode = -15

        def kill(self):
            events.append("kill-run-client")
            self.returncode = -9

        async def wait(self):
            events.append("reap-run-client")
            return self.returncode

    class CleanupProcess:
        def __init__(self):
            self.returncode = None

        async def communicate(self):
            events.append("remove-container")
            self.returncode = 0
            return b"", b""

        def kill(self):
            self.returncode = -9

    process = HangingProcess()

    async def spawn(*args, **kwargs):
        if args[1:3] == ("rm", "-f"):
            assert args[3] == "apdl-codegen-timeout"
            events.append("spawn-remove")
            return CleanupProcess()
        events.append("spawn-run")
        return process

    monkeypatch.setattr(
        "app.editor.container_editor.asyncio.create_subprocess_exec", spawn
    )

    rc, _out, err = await editor._run_docker(
        ["docker", "run"],
        {},
        container_name="apdl-codegen-timeout",
    )

    assert rc == 124
    assert "timed out" in err
    assert events == [
        "spawn-run",
        "terminate-run-client",
        "reap-run-client",
        "spawn-remove",
        "remove-container",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "container_name",
    [
        "apdl-codegen-inspect-repeat-cancel",
        "apdl-codegen-edit-repeat-cancel",
    ],
)
async def test_repeated_cancellation_cannot_interrupt_container_cleanup(
    monkeypatch,
    container_name,
):
    editor = ContainerAiderEditor()
    run_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class HangingProcess:
        returncode = None

        async def communicate(self, _stdin=None):
            run_started.set()
            await asyncio.Event().wait()

    process = HangingProcess()

    async def spawn(*_args, **_kwargs):
        return process

    async def cleanup(observed_name, observed_process):
        assert observed_name == container_name
        assert observed_process is process
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()

    monkeypatch.setattr(
        "app.editor.container_editor.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(editor, "_stop_container_and_client", cleanup)

    task = asyncio.create_task(
        editor._run_docker(
            ["docker", "run"],
            {},
            container_name=container_name,
        )
    )
    await run_started.wait()
    task.cancel()
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not cleanup_finished.is_set()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "container_name",
    [
        "apdl-codegen-inspect-spawn-cancel",
        "apdl-codegen-edit-spawn-cancel",
    ],
)
async def test_cancellation_during_spawn_still_removes_deterministic_container(
    monkeypatch,
    container_name,
):
    editor = ContainerAiderEditor()
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()
    removed = asyncio.Event()

    class SpawnedProcess:
        def __init__(self):
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        async def wait(self):
            return self.returncode

    process = SpawnedProcess()

    async def spawn(*_args, **_kwargs):
        spawn_started.set()
        await release_spawn.wait()
        return process

    async def control(*args):
        assert args == ("rm", "-f", container_name)
        removed.set()
        return 0, ""

    monkeypatch.setattr(
        "app.editor.container_editor.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(editor, "_docker_control_command", control)

    task = asyncio.create_task(
        editor._run_docker(
            ["docker", "run", "--name", container_name],
            {},
            container_name=container_name,
        )
    )
    await spawn_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release_spawn.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [0, 125])
async def test_completed_docker_client_always_removes_named_container(
    monkeypatch,
    returncode,
):
    editor = ContainerAiderEditor()
    container_name = "apdl-codegen-completed-client"
    removals: list[tuple[str, ...]] = []

    class CompletedProcess:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self):
            return b'{"success":true}', b"docker stream ended"

    async def spawn(*_args, **_kwargs):
        return CompletedProcess()

    async def control(*args):
        removals.append(args)
        return 0, ""

    monkeypatch.setattr(
        "app.editor.container_editor.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(editor, "_docker_control_command", control)

    result = await editor._run_docker(
        ["docker", "run", "--name", container_name],
        {},
        container_name=container_name,
    )

    assert result == (
        returncode,
        '{"success":true}',
        "docker stream ended",
    )
    assert removals == [("rm", "-f", container_name)]


@pytest.mark.asyncio
async def test_completed_docker_client_fails_closed_if_removal_is_unverified(
    monkeypatch,
):
    editor = ContainerAiderEditor()

    class CompletedProcess:
        returncode = 0

        async def communicate(self):
            return b'{"success":true}', b""

    async def spawn(*_args, **_kwargs):
        return CompletedProcess()

    async def control(*args):
        if args[0] == "inspect":
            return 0, "container still exists"
        return 1, "Docker daemon refused removal"

    monkeypatch.setattr(
        "app.editor.container_editor.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(editor, "_docker_control_command", control)

    with pytest.raises(RuntimeError, match="Could not verify removal"):
        await editor._run_docker(
            ["docker", "run", "--name", "apdl-codegen-stuck-client"],
            {},
            container_name="apdl-codegen-stuck-client",
        )


@pytest.mark.asyncio
async def test_container_cleanup_treats_verified_absence_as_success(monkeypatch):
    editor = ContainerAiderEditor()
    responses = iter(
        [
            (1, "remove failed"),
            (1, "Error: No such object: apdl-codegen-gone"),
        ]
    )

    async def control(*args):
        return next(responses)

    class ExitedClient:
        returncode = 0

    monkeypatch.setattr(editor, "_docker_control_command", control)

    await editor._stop_container_and_client(
        "apdl-codegen-gone", ExitedClient()
    )


@pytest.mark.asyncio
async def test_container_cleanup_raises_if_container_still_exists(monkeypatch, caplog):
    editor = ContainerAiderEditor()
    calls: list[tuple[str, ...]] = []

    async def control(*args):
        calls.append(args)
        if args[0] == "inspect":
            return 0, "container still exists"
        return 1, "Docker daemon refused removal"

    class ExitedClient:
        returncode = 0

    monkeypatch.setattr(editor, "_docker_control_command", control)

    with pytest.raises(RuntimeError, match="Could not verify removal"):
        await editor._stop_container_and_client(
            "apdl-codegen-stuck", ExitedClient()
        )

    assert calls == [
        ("rm", "-f", "apdl-codegen-stuck"),
        ("inspect", "--type", "container", "apdl-codegen-stuck"),
        ("rm", "-f", "apdl-codegen-stuck"),
        ("inspect", "--type", "container", "apdl-codegen-stuck"),
    ]
    assert "may still be running" in caplog.text


@pytest.mark.asyncio
async def test_implement_parses_a_successful_run(monkeypatch):
    editor = ContainerAiderEditor()
    preflight = json.dumps(
        {
            "success": True,
            "attestation": _req().repository_preflight.model_dump(mode="json"),
        }
    )
    payload = json.dumps(
        {"success": True, "branch": "apdl/x", "diff_stat": {"files": 1}}
    )
    container_names: list[str] = []
    launches: list[tuple[list[str], dict[str, str], bytes]] = []

    async def fake_run(_argv, _env, *, container_name, stdin_data):
        container_names.append(container_name)
        launches.append((_argv, _env, stdin_data))
        assert ["--name", container_name] == _argv[
            _argv.index("--name") : _argv.index("--name") + 2
        ]
        if "app.inspection.preflight_cli" in _argv:
            return 0, f"log line\n{preflight}\n", ""
        return 0, f"log line\n{payload}\n", "stderr noise"

    monkeypatch.setattr(editor, "_run_docker", fake_run)
    res = await editor.implement(_req())
    assert res.success is True
    assert res.diff_stat == {"files": 1}
    assert len(container_names) == 2
    assert container_names[0].startswith("apdl-codegen-inspect-")
    assert container_names[1].startswith("apdl-codegen-edit-")
    assert container_names[0] != container_names[1]
    assert json.loads(launches[0][2]) == {"read_token": "ghs_secrettoken"}
    worker_request = decode_codegen_worker_request(launches[1][2])
    assert worker_request.schema_version == "codegen_worker_request@1"
    assert worker_request.read_token == "ghs_secrettoken"
    assert worker_request.spec == "do the thing"
    assert "GH_TOKEN" not in launches[0][1]
    assert "GH_TOKEN" not in launches[1][1]


@pytest.mark.asyncio
async def test_failed_preflight_never_launches_provider_container(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret")
    editor = ContainerAiderEditor()
    launches: list[list[str]] = []

    async def fake_run(argv, _env, *, container_name, stdin_data):
        launches.append(argv)
        return (
            1,
            json.dumps(
                {
                    "success": False,
                    "error": "repository preflight refused: InspectionPathError",
                }
            ),
            "",
        )

    monkeypatch.setattr(editor, "_run_docker", fake_run)

    result = await editor.implement(_req())

    assert result.success is False
    assert "repository preflight failed" in (result.error or "")
    assert len(launches) == 1
    assert "app.inspection.preflight_cli" in launches[0]
    assert "ANTHROPIC_API_KEY" not in launches[0]
