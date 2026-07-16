"""Contract tests for the real, credential-minimal evaluation candidate.

These tests deliberately use an injected editor and pure Docker command builders.
They must never require a live model provider, Docker daemon, or GitHub repository.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.editor.base import EditResult
from app.evaluations.candidate import evaluate_candidate
from app.evaluations.corpus import DEFAULT_FIXTURE_ROOT, load_corpus
from app.evaluations.docker_executor import DockerEvaluationExecutor
from app.evaluations.fixtures import materialize_fixture, run_fixture_harness
from app.evaluations.models import (
    Ecosystem,
    EvidenceReference,
    EvidenceSource,
    HarnessObservation,
    MetricValue,
    RolloutPolicy,
)
from app.evaluations.runner import EvaluationInvocation
from app.evaluations.subprocess_executor import PublicEvaluationInvocation
from app.evaluations.subprocess_executor import SubprocessEvaluationExecutor
from app.requirements import compile_requirement_ledger, map_implementation_evidence


_INVOCATION_ID = "eval_inv_" + "a" * 32
_PINNED_IMAGE = "apdl-codegen-evaluator@sha256:" + "b" * 64
_PROVIDER_SECRET = "provider-secret-material"


def _task():
    case = next(
        item for item in load_corpus().cases if item.case_id == "python-version-drift"
    )
    return case.task


def _public_invocation() -> PublicEvaluationInvocation:
    return PublicEvaluationInvocation(
        invocation_id=_INVOCATION_ID,
        ecosystem=Ecosystem.python,
        task=_task(),
    )


def _materialized_python_fixture(tmp_path: Path):
    case = next(
        item for item in load_corpus().cases if item.case_id == "python-version-drift"
    )
    return materialize_fixture(
        case,
        tmp_path / "checkout",
        fixture_root=DEFAULT_FIXTURE_ROOT,
    )


class _RepairingEditor:
    """Keyless editor seam that makes the canonical repair in the supplied checkout."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def implement_workspace(
        self,
        request,
        workspace: Path,
    ) -> EditResult:
        self.requests.append(request)
        target = workspace / "app.py"
        target.write_text(
            "def send_signup(client):\n"
            "    client.capture(\"signup\")\n",
            encoding="utf-8",
        )
        diff = subprocess.run(
            ["git", "diff", "--", "app.py"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        task = getattr(request, "task", None)
        title = task.title if task is not None else request.title
        spec = task.spec if task is not None else request.spec
        constraints = task.constraints if task is not None else request.constraints
        risk = task.risk.value if task is not None else request.risk_level
        ledger = compile_requirement_ledger(
            title=title,
            spec=spec,
            constraints=constraints,
            risk=risk,
            verification_command=None,
        )
        ledger = map_implementation_evidence(ledger, ["app.py"])
        return EditResult(
            success=True,
            changed_paths=["app.py"],
            diff_stat={"files": 1, "additions": 1, "deletions": 1},
            diff_text=diff,
            head_sha=subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            requirement_ledger=ledger,
            prompts=[
                {
                    "stage": "edit",
                    "label": "Edit instruction (attempt 1)",
                    "system": None,
                    "user": spec,
                    "notes": "keyless test editor",
                }
            ],
        )


@pytest.mark.asyncio
async def test_evaluate_candidate_edits_real_fixture_with_keyless_injected_editor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    materialized, manifest = _materialized_python_fixture(tmp_path)
    assert run_fixture_harness(materialized, manifest).passed is False

    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_APP_PRIVATE_KEY",
        "POSTGRES_URL",
        "DATABASE_URL",
        "REDIS_URL",
        "SSH_AUTH_SOCK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(materialized.workspace)
    editor = _RepairingEditor()

    execution = await evaluate_candidate(
        _public_invocation(),
        workspace=materialized.workspace,
        editor=editor,
    )

    assert execution.schema_version == "evaluation_execution@2"
    assert execution.invocation_id == _INVOCATION_ID
    assert len(editor.requests) == 1
    assert run_fixture_harness(materialized, manifest).passed is True
    assert execution.measurements.requirement_coverage.value == 1.0
    assert execution.measurements.retries.value == 0.0
    assert execution.measurements.latency_seconds.value is not None
    assert execution.measurements.first_pass_ci_success.value is None
    assert execution.measurements.ci_repair_success.value is None
    assert execution.measurements.reverted.value is None
    assert execution.measurements.human_correction_lines.value is None
    assert execution.measurements.cost_usd.value is None
    assert execution.reviewer is None
    assert all(item.channel.value != "github_ci" for item in execution.detections)

    for field_name in type(execution.measurements).model_fields:
        metric = getattr(execution.measurements, field_name)
        if metric.value is None:
            assert metric.unavailable_reason
            assert metric.evidence == []
        else:
            assert metric.unavailable_reason is None
            assert metric.evidence
            assert all(item.sha256 is not None for item in metric.evidence)

    serialized = execution.model_dump_json()
    assert _PROVIDER_SECRET not in serialized
    assert "github_pat_" not in serialized


def test_fixture_harness_is_semantic_and_binds_the_final_tree(tmp_path: Path):
    materialized, manifest = _materialized_python_fixture(tmp_path)
    target = materialized.workspace / "app.py"

    initial = run_fixture_harness(materialized, manifest)
    assert initial.passed is False

    target.write_text(
        target.read_text(encoding="utf-8") + "\n# unrelated candidate comment\n",
        encoding="utf-8",
    )
    changed_tree = run_fixture_harness(materialized, manifest)
    assert changed_tree.passed is False
    assert changed_tree.evidence_sha256 != initial.evidence_sha256

    target.write_text(
        target.read_text(encoding="utf-8")
        + "# client.capture(\"signup\") is the intended repair\n",
        encoding="utf-8",
    )
    comment_only = run_fixture_harness(materialized, manifest)
    assert comment_only.passed is False
    assert comment_only.evidence_sha256 != changed_tree.evidence_sha256


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MetricValue(
            value="1",
            evidence=[
                EvidenceReference(source=EvidenceSource.executor, reference="evidence://1")
            ],
        ),
        lambda: MetricValue(
            value=True,
            evidence=[
                EvidenceReference(source=EvidenceSource.executor, reference="evidence://1")
            ],
        ),
        lambda: RolloutPolicy(minimum_sample_size=True),
        lambda: HarnessObservation(
            fixture_id="strict-types",
            fixture_sha256="1" * 64,
            passed=1,
            assertions_total=1,
            assertions_passed=1,
            failing_assertion_ids=[],
            evidence_sha256="2" * 64,
        ),
    ],
)
def test_evaluation_contracts_reject_coercive_input(factory):
    with pytest.raises(ValidationError):
        factory()


def test_docker_evaluator_argv_and_environment_are_credential_minimal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    materialized, _manifest = _materialized_python_fixture(tmp_path)
    workspace = materialized.workspace
    invocation = EvaluationInvocation(
        invocation_id=_INVOCATION_ID,
        ecosystem=Ecosystem.python,
        task=_task(),
        workspace=workspace,
    )
    source_environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "OPENAI_API_KEY": _PROVIDER_SECRET,
        "GITHUB_TOKEN": "github-write-token",
        "GH_TOKEN": "gh-write-token",
        "GITHUB_APP_PRIVATE_KEY": "private-key",
        "POSTGRES_URL": "postgresql://secret",
        "DATABASE_URL": "postgresql://secret",
        "REDIS_URL": "redis://secret",
        "SSH_AUTH_SOCK": "/host/agent.sock",
        "APDL_INTERNAL_TOKEN": "internal-token",
    }
    for name, value in source_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CODEGEN_EVALUATION_NETWORK", "apdl-codegen-egress-filtered")

    executor = DockerEvaluationExecutor(image=_PINNED_IMAGE)
    argv = executor._docker_argv(
        invocation,
        container_name="apdl-evaluation-contract-test",
    )
    environment = executor._docker_environment()
    rendered = "\0".join(argv)

    assert argv[1:3] == ["run", "--rm"]
    assert "--read-only" in argv
    assert "--cap-drop" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv
    assert "no-new-privileges" in argv
    assert "--pids-limit" in argv
    assert "--memory" in argv
    assert "--cpus" in argv
    assert "--user" in argv
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "apdl-codegen-egress-filtered"
    assert str(workspace.resolve()) in rendered
    assert _PINNED_IMAGE in argv
    assert "OPENAI_API_KEY" in argv
    assert _PROVIDER_SECRET not in rendered
    assert "--privileged" not in argv
    assert "docker.sock" not in rendered

    assert environment["OPENAI_API_KEY"] == _PROVIDER_SECRET
    for forbidden in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_APP_PRIVATE_KEY",
        "POSTGRES_URL",
        "DATABASE_URL",
        "REDIS_URL",
        "SSH_AUTH_SOCK",
        "APDL_INTERNAL_TOKEN",
    ):
        assert forbidden not in environment
        assert source_environment[forbidden] not in rendered


def test_docker_evaluator_requires_an_immutable_image_reference():
    with pytest.raises(ValueError, match="digest|immutable|sha256"):
        DockerEvaluationExecutor(image="apdl-codegen-evaluator:latest")


@pytest.mark.asyncio
async def test_installed_candidate_protocol_runs_real_workspace_editor_path(
    tmp_path: Path,
):
    """Exercise stdin/stdout + AiderEditor with a deterministic fake Aider binary."""
    corpus = load_corpus()
    case = next(
        item
        for item in corpus.cases
        if item.case_id == "python-flaky-infrastructure"
    )
    materialized, manifest = materialize_fixture(
        case,
        tmp_path / "boundary" / "checkout",
        fixture_root=DEFAULT_FIXTURE_ROOT,
    )
    fake_aider = tmp_path / "fake-aider"
    fake_aider.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "path = Path('classifier.py')\n"
        "text = path.read_text(encoding='utf-8')\n"
        "path.write_text(text.replace(\n"
        "    'if failure == \"runner_timeout\":\\n'\n"
        "    '        return \"product_code_repair\"',\n"
        "    'if failure == \"runner_timeout\":\\n'\n"
        "    '        return \"infrastructure_rerun\"',\n"
        "), encoding='utf-8')\n"
        "subprocess.run(['git', 'add', 'classifier.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'fix classifier'], check=True)\n",
        encoding="utf-8",
    )
    fake_aider.chmod(0o755)
    executor = SubprocessEvaluationExecutor(
        [sys.executable, "-m", "app.evaluations.candidate"],
        timeout_seconds=30,
        environment={
            "PATH": os.environ.get("PATH", os.defpath),
            "CODEGEN_MODEL": "keyless-test-model",
            "CODEGEN_REVISION": "keyless-test-revision",
            "CODEGEN_AIDER_BIN": str(fake_aider),
            "CODEGEN_BRIEF": "false",
            "CODEGEN_REVIEW": "false",
            "CODEGEN_CONTRACTS": "false",
            "CODEGEN_CONVENTIONS": "false",
            "CODEGEN_CACHE_PROMPTS": "false",
        },
    )
    execution = await executor.execute(
        EvaluationInvocation(
            invocation_id=_INVOCATION_ID,
            ecosystem=case.ecosystem,
            task=case.task,
            workspace=materialized.workspace,
        )
    )

    assert execution.invocation_id == _INVOCATION_ID
    assert execution.measurements.requirement_coverage.value == 1.0
    assert run_fixture_harness(materialized, manifest).passed is True
