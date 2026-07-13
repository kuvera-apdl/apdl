"""Tests for the sandboxed (container) editor's pure logic + never-raise contract.

The real path (an actual `docker run` of the sandbox image) is integration-
untested; these cover argv/env assembly, the secrets-off-argv property, result
parsing, and that an attempt never raises.
"""

import asyncio
import json

import pytest

from app.contracts.models import ContractBundle
from app.editor.base import EditRequest
from app.editor.container_editor import ContainerAiderEditor, _last_json
from app.inspection.models import DependencySlice, InspectionSnapshot
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


def test_docker_argv_passes_nonsecret_inputs_as_values():
    argv = " ".join(ContainerAiderEditor()._docker_argv(_req()))
    assert "CS_REPO=acme/widgets" in argv
    assert "CS_PROJECT_SCOPE=acme/widgets" in argv
    assert "CS_BRANCH=apdl/x" in argv
    assert "CS_TEST_CMD=python -m pytest -q" in argv
    assert 'CS_CONSTRAINTS=["keep tests green"]' in argv
    assert "HOME=/workspace/home" in argv


def test_docker_argv_omits_test_cmd_when_unset():
    argv = " ".join(ContainerAiderEditor()._docker_argv(_req(test_cmd=None)))
    assert "CS_TEST_CMD" not in argv


def test_docker_argv_passes_effective_safety_policy_and_revert_sha():
    safety_policy = resolve_effective_policy(
        TenantCodegenConnectionPolicy(
            gates=TenantCodegenGatesPolicy(max_files=5)
        ),
        PlatformCodegenSafetyPolicy(),
    )
    req = _req(safety_policy=safety_policy, revert_sha="cafebabe")
    argv = " ".join(ContainerAiderEditor()._docker_argv(req))
    assert (
        "CS_SAFETY_POLICY="
        + json.dumps(safety_policy.model_dump(mode="json"))
    ) in argv
    assert f"CS_SAFETY_POLICY_SHA256={safety_policy.canonical_digest()}" in argv
    assert "CS_GATES_POLICY" not in argv
    assert "CS_REVERT_SHA=cafebabe" in argv


def test_docker_argv_forwards_editor_config(monkeypatch):
    # The sandboxed AiderEditor must behave exactly like the in-process one:
    # operator knobs (fail-closed posture, timeouts, pass toggles) ride along.
    monkeypatch.setenv("CODEGEN_REQUIRE_VERIFY", "false")
    monkeypatch.setenv("CODEGEN_CONVENTIONS", "false")
    argv = " ".join(ContainerAiderEditor()._docker_argv(_req()))
    assert "CODEGEN_REQUIRE_VERIFY=false" in argv
    assert "CODEGEN_CONVENTIONS=false" in argv


def test_container_timeout_covers_the_full_job_budget(monkeypatch):
    # The container runs the whole pipeline, but its credential-bearing wall
    # time remains below GitHub's one-hour installation-token lifetime.
    monkeypatch.setenv("CODEGEN_TIMEOUT", "1800")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "300")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    assert ContainerAiderEditor()._timeout == 3000


def test_secrets_are_passed_by_name_not_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")
    editor = ContainerAiderEditor()
    argv = editor._docker_argv(_req())
    joined = " ".join(argv)
    # The token + key NAMES are forwarded...
    assert "GH_TOKEN" in argv and "ANTHROPIC_API_KEY" in argv
    # ...but their VALUES never touch the argv.
    assert "ghs_secrettoken" not in joined
    assert "sk-ant-secretvalue" not in joined
    # The App private key is never forwarded at all.
    assert "GITHUB_APP_PRIVATE_KEY" not in argv


def test_docker_env_carries_secrets_but_not_internal(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secretvalue")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    monkeypatch.setenv("APDL_INTERNAL_TOKEN", "internal")
    env = ContainerAiderEditor()._docker_env(_req())
    assert env["GH_TOKEN"] == "ghs_secrettoken"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-secretvalue"
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert "APDL_INTERNAL_TOKEN" not in env


def test_last_json_finds_result_among_noise():
    blob = "cloning...\nrunning aider\n" + json.dumps({"success": True, "branch": "b"})
    assert _last_json(blob) == {"success": True, "branch": "b"}
    assert _last_json("no json here\n{bad json}") is None


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
    payload = json.dumps({"success": True, "branch": "apdl/x", "diff_stat": {"files": 1}})
    container_names: list[str] = []

    async def fake_run(_argv, _env, *, container_name):
        container_names.append(container_name)
        assert ["--name", container_name] == _argv[
            _argv.index("--name") : _argv.index("--name") + 2
        ]
        return 0, f"log line\n{payload}\n", "stderr noise"

    monkeypatch.setattr(editor, "_run_docker", fake_run)
    res = await editor.implement(_req())
    assert res.success is True
    assert res.diff_stat == {"files": 1}
    assert len(container_names) == 1
    assert container_names[0].startswith("apdl-codegen-")
