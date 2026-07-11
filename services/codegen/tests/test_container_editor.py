"""Tests for the sandboxed (container) editor's pure logic + never-raise contract.

The real path (an actual `docker run` of the sandbox image) is integration-
untested; these cover argv/env assembly, the secrets-off-argv property, result
parsing, and that an attempt never raises.
"""

import json

import pytest

from app.contracts.models import ContractBundle
from app.editor.base import EditRequest
from app.editor.container_editor import ContainerAiderEditor, _last_json
from app.inspection.models import DependencySlice, InspectionSnapshot
from app.profiling import RepoProfile
from app.requirements import compile_requirement_ledger
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
    assert "--pids-limit" in argv and "--memory" in argv and "--cpus" in argv
    assert argv[-1] == "apdl-sandbox:test"  # image is the final arg


def test_docker_argv_passes_nonsecret_inputs_as_values():
    argv = " ".join(ContainerAiderEditor()._docker_argv(_req()))
    assert "CS_REPO=acme/widgets" in argv
    assert "CS_PROJECT_SCOPE=acme/widgets" in argv
    assert "CS_BRANCH=apdl/x" in argv
    assert "CS_TEST_CMD=python -m pytest -q" in argv
    assert 'CS_CONSTRAINTS=["keep tests green"]' in argv


def test_docker_argv_omits_test_cmd_when_unset():
    argv = " ".join(ContainerAiderEditor()._docker_argv(_req(test_cmd=None)))
    assert "CS_TEST_CMD" not in argv


def test_docker_argv_passes_gates_policy_and_revert_sha():
    req = _req(gates_policy={"max_files": 5}, revert_sha="cafebabe")
    argv = " ".join(ContainerAiderEditor()._docker_argv(req))
    assert 'CS_GATES_POLICY={"max_files": 5}' in argv
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
    # The container runs the WHOLE pipeline (retry rounds included); capping it
    # at the bare agent timeout would kill legitimate retries mid-run.
    monkeypatch.setenv("CODEGEN_TIMEOUT", "1800")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "300")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    assert ContainerAiderEditor()._timeout == 2 * 1800 + 2 * 300


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
async def test_implement_parses_a_successful_run(monkeypatch):
    editor = ContainerAiderEditor()
    payload = json.dumps({"success": True, "branch": "apdl/x", "diff_stat": {"files": 1}})

    async def fake_run(_argv, _env):
        return 0, f"log line\n{payload}\n", "stderr noise"

    monkeypatch.setattr(editor, "_run_docker", fake_run)
    res = await editor.implement(_req())
    assert res.success is True
    assert res.diff_stat == {"files": 1}
