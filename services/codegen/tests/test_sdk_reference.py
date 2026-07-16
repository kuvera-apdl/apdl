"""Tests for language-scoped APDL SDK reference detection."""

import pytest

from app.editor.sdk_reference import (
    JS_SDK_REFERENCE_MD,
    PYTHON_SDK_REFERENCE_MD,
    detect_sdk_references,
)
from app.inspection.repository import InspectionPathError


def _names(refs):
    return [name for name, _ in refs]


def test_detects_js_sdk_from_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@apdl-oss/sdk": "^1.0.0", "react": "^18"}}'
    )
    refs = detect_sdk_references(tmp_path)
    assert _names(refs) == ["APDL_SDK_JS.md"]
    assert refs[0][1] == JS_SDK_REFERENCE_MD


def test_detects_js_sdk_from_dev_dependencies(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"@apdl-oss/sdk": "^1.0.0"}}'
    )
    assert _names(detect_sdk_references(tmp_path)) == ["APDL_SDK_JS.md"]


def test_detects_js_sdk_in_large_bounded_manifest(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"description":"'
        + ("x" * 140_000)
        + '","dependencies":{"@apdl-oss/sdk":"^1.0.0"}}'
    )

    assert _names(detect_sdk_references(tmp_path)) == ["APDL_SDK_JS.md"]


def test_no_js_ref_when_sdk_absent(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18"}}')
    assert detect_sdk_references(tmp_path) == []


def test_ignores_malformed_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    assert detect_sdk_references(tmp_path) == []


def test_detects_python_sdk_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["fastapi", "apdl>=0.1", "httpx"]\n'
    )
    refs = detect_sdk_references(tmp_path)
    assert _names(refs) == ["APDL_SDK_PYTHON.md"]
    assert refs[0][1] == PYTHON_SDK_REFERENCE_MD


def test_detects_python_sdk_from_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi==0.110\napdl==0.1.0\n")
    assert _names(detect_sdk_references(tmp_path)) == ["APDL_SDK_PYTHON.md"]


def test_detects_python_sdk_bare_name(tmp_path):
    (tmp_path / "requirements.txt").write_text("apdl\nhttpx\n")
    assert _names(detect_sdk_references(tmp_path)) == ["APDL_SDK_PYTHON.md"]


def test_python_detection_does_not_false_match_sibling_packages(tmp_path):
    # `apdl-codegen` / `myapdl` / `apdlx` are NOT the `apdl` SDK.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["apdl-codegen", "myapdl", "apdlx"]\n'
    )
    assert detect_sdk_references(tmp_path) == []


def test_detects_both_sdks_in_full_stack_repo(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@apdl-oss/sdk": "^1.0.0"}}'
    )
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["apdl"]\n')
    assert _names(detect_sdk_references(tmp_path)) == [
        "APDL_SDK_JS.md",
        "APDL_SDK_PYTHON.md",
    ]


def test_no_refs_for_repo_without_apdl(tmp_path):
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "^18"}}')
    (tmp_path / "requirements.txt").write_text("flask\n")
    assert detect_sdk_references(tmp_path) == []


def test_sdk_detection_rejects_manifest_symlink_to_outside(tmp_path):
    outside = tmp_path.parent / "outside-package.json"
    outside.write_text(
        '{"dependencies":{"@apdl-oss/sdk":"1"},'
        '"token":"provider-secret-that-must-not-be-read"}',
        encoding="utf-8",
    )
    (tmp_path / "package.json").symlink_to(outside)

    with pytest.raises(
        InspectionPathError, match="repository contains a symbolic link"
    ):
        detect_sdk_references(tmp_path)
