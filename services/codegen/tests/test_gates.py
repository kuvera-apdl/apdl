"""Unit tests for the deterministic pre-push safety gates."""

import pytest

from app.runtime.models import RUNTIME_ACCEPTANCE_WORKFLOW_PATH
from app.safety.gates import (
    diff_too_large,
    evaluate_pre_push,
    protected_path_violations,
    scan_secrets,
)
from app.safety.policy import (
    EffectiveCodegenSafetyPolicy,
    VerifiedProtectedPathExemption,
)


def _policy(**overrides) -> EffectiveCodegenSafetyPolicy:
    return EffectiveCodegenSafetyPolicy(**overrides)


def _stat(*, files: int = 1, additions: int = 0, deletions: int = 0) -> dict:
    return {"files": files, "additions": additions, "deletions": deletions}


def _runtime_workflow_exemption() -> VerifiedProtectedPathExemption:
    return VerifiedProtectedPathExemption(
        content_sha256="a" * 64,
        runtime_acceptance_plan_sha256="b" * 64,
    )


def test_scan_secrets_flags_known_shapes():
    assert scan_secrets("AKIAIOSFODNN7EXAMPLE")
    assert scan_secrets("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert scan_secrets("token=ghp_" + "a" * 40)
    assert scan_secrets("a clean diff with no secrets") == []


def test_protected_paths_flag_ci_and_env_only():
    violations = protected_path_violations(
        [".github/workflows/ci.yml", "src/app.py", "config/.env"]
    )
    assert any("ci.yml" in message for message in violations)
    assert any(".env" in message for message in violations)
    assert len(violations) == 2


def test_diff_too_large_caps_files_and_lines():
    assert diff_too_large(_stat(files=80)) is not None
    assert diff_too_large(_stat(files=2, additions=5000)) is not None
    assert diff_too_large(_stat(files=2, additions=10, deletions=5)) is None


@pytest.mark.parametrize(
    "diff_stat",
    [
        None,
        {},
        {"files": 1, "additions": 1},
        {"files": "1", "additions": 1, "deletions": 0},
        {"files": True, "additions": 1, "deletions": 0},
        {"files": -1, "additions": 1, "deletions": 0},
    ],
)
def test_diff_too_large_fails_closed_for_malformed_stats(diff_stat):
    assert "malformed" in diff_too_large(diff_stat).lower()


def test_evaluate_pre_push_aggregates_violations():
    result = evaluate_pre_push(
        diff_stat=_stat(files=2, additions=10),
        changed_paths=["src/app.py", ".github/workflows/ci.yml"],
        diff_text="AKIAIOSFODNN7EXAMPLE",
        policy=_policy(),
    )
    assert result.passed is False
    assert len(result.violations) == 2


def test_evaluate_pre_push_passes_clean_diff_and_respects_effective_policy():
    clean = evaluate_pre_push(
        diff_stat=_stat(files=1, additions=3),
        changed_paths=["src/app.py"],
        diff_text="all good",
        policy=_policy(),
    )
    assert clean.passed is True

    tight = evaluate_pre_push(
        diff_stat=_stat(files=5),
        changed_paths=[],
        policy=_policy(max_files=3),
    )
    assert tight.passed is False


def test_evaluate_pre_push_rejects_raw_tenant_dictionary():
    with pytest.raises(TypeError, match="EffectiveCodegenSafetyPolicy"):
        evaluate_pre_push(
            diff_stat=_stat(),
            changed_paths=[],
            policy={"protected_paths": [], "max_files": 10_000_000},
        )


def test_verified_exemption_allows_only_reserved_generated_workflow():
    result = evaluate_pre_push(
        diff_stat=_stat(files=2),
        changed_paths=[
            RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
            ".github/workflows/existing-ci.yml",
        ],
        policy=_policy(runtime_workflow_generation_enabled=True),
        verified_exemptions=(_runtime_workflow_exemption(),),
    )

    assert result.passed is False
    assert len(result.violations) == 1
    assert "existing-ci.yml" in result.violations[0]


def test_verified_exemption_requires_effective_runtime_grant():
    result = evaluate_pre_push(
        diff_stat=_stat(),
        changed_paths=[RUNTIME_ACCEPTANCE_WORKFLOW_PATH],
        policy=_policy(runtime_workflow_generation_enabled=False),
        verified_exemptions=(_runtime_workflow_exemption(),),
    )

    assert result.passed is False
    assert any("not enabled" in violation for violation in result.violations)
    assert any("protected path" in violation for violation in result.violations)


def test_secret_scan_remains_active_with_verified_path_exemption():
    result = evaluate_pre_push(
        diff_stat=_stat(),
        changed_paths=[RUNTIME_ACCEPTANCE_WORKFLOW_PATH],
        diff_text="token=ghp_" + "a" * 40,
        policy=_policy(runtime_workflow_generation_enabled=True),
        verified_exemptions=(_runtime_workflow_exemption(),),
    )

    assert result.passed is False
    assert len(result.violations) == 1
    assert "secret" in result.violations[0].lower()
