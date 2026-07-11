"""Unit tests for the deterministic pre-push safety gates."""

from app.safety.gates import (
    diff_too_large,
    evaluate_pre_push,
    protected_path_violations,
    scan_secrets,
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
    assert any("ci.yml" in m for m in violations)
    assert any(".env" in m for m in violations)
    assert len(violations) == 2  # src/app.py is not protected


def test_diff_too_large_caps_files_and_lines():
    assert diff_too_large({"files": 80}) is not None
    assert diff_too_large({"files": 2, "additions": 5000}) is not None
    assert diff_too_large({"files": 2, "additions": 10, "deletions": 5}) is None


def test_evaluate_pre_push_aggregates_violations():
    result = evaluate_pre_push(
        diff_stat={"files": 2, "additions": 10},
        changed_paths=["src/app.py", ".github/workflows/ci.yml"],
        diff_text="AKIAIOSFODNN7EXAMPLE",
    )
    assert result.passed is False
    assert len(result.violations) == 2  # protected path + secret


def test_evaluate_pre_push_passes_clean_diff_and_respects_policy():
    clean = evaluate_pre_push(
        diff_stat={"files": 1, "additions": 3},
        changed_paths=["src/app.py"],
        diff_text="all good",
    )
    assert clean.passed is True

    tight = evaluate_pre_push(
        diff_stat={"files": 5}, changed_paths=[], policy={"max_files": 3}
    )
    assert tight.passed is False


def test_explicit_policy_allows_only_the_named_generated_workflow():
    result = evaluate_pre_push(
        diff_stat={"files": 2},
        changed_paths=[
            ".github/workflows/apdl-runtime-acceptance.yml",
            ".github/workflows/existing-ci.yml",
        ],
        policy={
            "allowed_protected_paths": [
                ".github/workflows/apdl-runtime-acceptance.yml"
            ]
        },
    )

    assert result.passed is False
    assert len(result.violations) == 1
    assert "existing-ci.yml" in result.violations[0]
