"""Tests for env-derived config — focused on GitHub App private-key resolution.

The key must load cleanly from a single-line ``.env`` value (the Docker case) as
well as from a file, so these cover inline (incl. escaped newlines), base64, the
``~``-expanded path, precedence, and the empty fallback.
"""

import base64

import pytest

from app import config
from app.editor.environment import codegen_behavior_configuration_sha256
from app.evaluations.models import CodegenCandidateIdentity, RiskLevel, RolloutStage
from app.editor.aider_editor import AiderEditor
from app.editor.container_editor import ContainerAiderEditor
from app.main import _make_editor, _make_publication_gate
from app.publication import (
    DEVELOPMENT_CODEGEN_REVISION,
    DevelopmentPublicationAuthorization,
)

_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBVQIBADAN\n-----END RSA PRIVATE KEY-----\n"

_KEY_VARS = (
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_BASE64",
    "GITHUB_APP_PRIVATE_KEY_PATH",
)


def _clear(monkeypatch):
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_inline_key_returned_trimmed(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PEM)
    assert config.github_app_private_key() == _PEM.strip()


def test_inline_key_unescapes_single_line_newlines(monkeypatch):
    _clear(monkeypatch)
    # The shape a PEM takes when squeezed onto one .env line.
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _PEM.replace("\n", "\\n"))
    resolved = config.github_app_private_key()
    assert "\n" in resolved and "\\n" not in resolved
    assert resolved == _PEM.strip()


def test_base64_key_is_decoded(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(
        "GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(_PEM.encode()).decode()
    )
    assert config.github_app_private_key() == _PEM


def test_path_key_expands_tilde(monkeypatch, tmp_path):
    _clear(monkeypatch)
    (tmp_path / "key.pem").write_text(_PEM)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "~/key.pem")
    assert config.github_app_private_key() == _PEM


def test_inline_beats_base64_and_path(monkeypatch, tmp_path):
    (tmp_path / "k.pem").write_text("FROM_PATH")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "FROM_INLINE")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(b"FROM_B64").decode())
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "k.pem"))
    assert config.github_app_private_key() == "FROM_INLINE"


def test_base64_beats_path(monkeypatch, tmp_path):
    _clear(monkeypatch)
    (tmp_path / "k.pem").write_text("FROM_PATH")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", base64.b64encode(b"FROM_B64").decode())
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "k.pem"))
    assert config.github_app_private_key() == "FROM_B64"


def test_empty_when_nothing_set(monkeypatch):
    _clear(monkeypatch)
    assert config.github_app_private_key() == ""


def test_invalid_base64_falls_back_to_empty(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_BASE64", "not%%%base64%%%")
    assert config.github_app_private_key() == ""


def test_cors_origins_default_to_local_admin(monkeypatch):
    monkeypatch.delenv("CODEGEN_CORS_ORIGINS", raising=False)
    origins = config.codegen_cors_origins()
    assert "http://localhost:5174" in origins
    assert "*" not in origins  # never wildcard — this service merges PRs


def test_cors_origins_parsed_from_env(monkeypatch):
    monkeypatch.setenv("CODEGEN_CORS_ORIGINS", "https://admin.example.com, https://ops.example.com ")
    assert config.codegen_cors_origins() == [
        "https://admin.example.com",
        "https://ops.example.com",
    ]


def test_ci_poll_interval_default_and_disable(monkeypatch):
    monkeypatch.delenv("CODEGEN_CI_POLL_INTERVAL", raising=False)
    assert config.codegen_ci_poll_interval() == 60
    monkeypatch.setenv("CODEGEN_CI_POLL_INTERVAL", "0")
    assert config.codegen_ci_poll_interval() == 0


def test_ci_repair_limits_default_and_floor(monkeypatch):
    monkeypatch.delenv("CODEGEN_CI_REPAIR_RETRIES", raising=False)
    monkeypatch.delenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", raising=False)
    assert config.codegen_ci_repair_retries() == 2
    assert config.codegen_ci_repair_budget_seconds() == 3600

    monkeypatch.setenv("CODEGEN_CI_REPAIR_RETRIES", "-1")
    monkeypatch.setenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "-1")
    assert config.codegen_ci_repair_retries() == 0
    assert config.codegen_ci_repair_budget_seconds() == 0


def test_job_budget_caps_derived_pipeline_below_github_token_ttl(monkeypatch):
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    monkeypatch.setenv("CODEGEN_TIMEOUT", "1800")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "300")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    assert config.codegen_job_budget() == config.MAX_CODEGEN_JOB_BUDGET_SECONDS


def test_job_budget_env_override_can_tighten_but_not_expand_token_bound(monkeypatch):
    monkeypatch.setenv("CODEGEN_JOB_BUDGET", "2400")
    assert config.codegen_job_budget() == 2400

    monkeypatch.setenv("CODEGEN_JOB_BUDGET", "7200")
    with pytest.raises(ValueError, match="cannot exceed 3000 seconds"):
        config.codegen_job_budget()


def test_stale_sweep_interval_default_and_disable(monkeypatch):
    monkeypatch.delenv("CODEGEN_STALE_SWEEP_INTERVAL", raising=False)
    assert config.codegen_stale_sweep_interval() == 300
    monkeypatch.setenv("CODEGEN_STALE_SWEEP_INTERVAL", "0")
    assert config.codegen_stale_sweep_interval() == 0


def test_rollout_config_defaults_fail_closed_and_binds_revision(monkeypatch):
    monkeypatch.delenv("CODEGEN_ROLLOUT_STAGE", raising=False)
    monkeypatch.delenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", raising=False)
    monkeypatch.delenv("CODEGEN_REVISION", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)

    assert config.codegen_rollout_stage() is RolloutStage.offline
    assert config.codegen_rollout_authorization_path() == ""
    assert config.codegen_revision() == "development-unversioned"

    monkeypatch.setenv("CODEGEN_REVISION", "image@sha256:abc")
    assert config.codegen_revision() == "image@sha256:abc"


def test_rollout_stage_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "automatic_merge")
    with pytest.raises(ValueError, match="CODEGEN_ROLLOUT_STAGE"):
        config.codegen_rollout_stage()


def test_development_mode_requires_explicit_true(monkeypatch):
    monkeypatch.delenv("CODEGEN_DEVELOPMENT_MODE", raising=False)
    assert config.codegen_development_mode() is False
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    assert config.codegen_development_mode() is True
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "false")
    assert config.codegen_development_mode() is False


def test_development_publication_gate_is_explicit_and_unevaluated(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "development_pr")
    monkeypatch.setenv("CODEGEN_REVISION", DEVELOPMENT_CODEGEN_REVISION)
    monkeypatch.delenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", raising=False)
    monkeypatch.delenv("CODEGEN_DEVELOPMENT_MODE", raising=False)

    with pytest.raises(RuntimeError, match="CODEGEN_DEVELOPMENT_MODE=true"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    gate = _make_publication_gate()
    authorization = gate.authorize(risk=RiskLevel.low, canary_identity="ignored")

    assert gate.stage is RolloutStage.development_pr
    assert isinstance(authorization, DevelopmentPublicationAuthorization)
    assert authorization.request.codegen_revision == DEVELOPMENT_CODEGEN_REVISION
    assert authorization.decision.ready_for_review is False
    assert authorization.draft_only is True
    assert "report_sha256" not in authorization.model_dump(mode="json")


def test_development_publication_rejects_bundle_and_non_dev_revision(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "development_pr")
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    monkeypatch.setenv("CODEGEN_REVISION", DEVELOPMENT_CODEGEN_REVISION)
    monkeypatch.setenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", "/bundle.json")
    with pytest.raises(RuntimeError, match="must not receive"):
        _make_publication_gate()

    monkeypatch.delenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", raising=False)
    monkeypatch.setenv("CODEGEN_REVISION", "production-revision")
    with pytest.raises(ValueError, match="local-development"):
        _make_publication_gate()


def test_development_marker_is_rejected_for_offline_stage(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "offline")
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    with pytest.raises(RuntimeError, match="valid only with development_pr"):
        _make_publication_gate()


def test_publication_gate_requires_operator_artifact_for_pr_stages(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "reviewed_pr")
    monkeypatch.delenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", raising=False)
    with pytest.raises(RuntimeError, match="AUTHORIZATION_PATH"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", "relative.json")
    with pytest.raises(RuntimeError, match="absolute path"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "offline")
    gate = _make_publication_gate()
    assert gate.stage is RolloutStage.offline
    assert gate.provider is None


def test_publication_gate_binds_exact_images_and_effective_behavior(monkeypatch):
    controller = "sha256:" + "a" * 64
    candidate = "sha256:" + "b" * 64
    captured: dict[str, str] = {}
    provider = object()

    def fake_loader(_path, **kwargs):
        captured.update(kwargs)
        return provider

    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "reviewed_pr")
    monkeypatch.setenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", "/bundle.json")
    monkeypatch.setenv("CODEGEN_MODEL", "test-model@1")
    monkeypatch.setenv("CODEGEN_HELPER_MODEL", "test-helper@1")
    monkeypatch.setenv("CODEGEN_REVISION", "evaluated-revision")
    monkeypatch.setenv("CODEGEN_CONTROLLER_IMAGE_ID", controller)
    monkeypatch.setenv("CODEGEN_SANDBOX_IMAGE", candidate)
    monkeypatch.setattr("app.main.load_publication_authorizer", fake_loader)

    gate = _make_publication_gate()
    identity = CodegenCandidateIdentity.build(
        controller_image_id=controller,
        candidate_image_id=candidate,
        codegen_revision="evaluated-revision",
        behavior_configuration_sha256=codegen_behavior_configuration_sha256(),
    )

    assert captured == {
        "expected_model": "test-model@1",
        "expected_codegen_revision": "evaluated-revision",
        "expected_candidate_identity_sha256": identity.identity_sha256,
    }
    assert gate.provider is provider
    assert gate.candidate_identity_sha256 == identity.identity_sha256


def test_publication_gate_rejects_mutable_image_identity(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "reviewed_pr")
    monkeypatch.setenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", "/bundle.json")
    monkeypatch.setenv("CODEGEN_REVISION", "evaluated-revision")
    monkeypatch.setenv("CODEGEN_CONTROLLER_IMAGE_ID", "controller:latest")
    monkeypatch.setenv("CODEGEN_SANDBOX_IMAGE", "candidate:latest")

    with pytest.raises(ValueError, match="validation errors"):
        _make_publication_gate()


def test_editor_defaults_to_isolated_container(monkeypatch):
    monkeypatch.delenv("CODEGEN_SANDBOX", raising=False)
    editor = _make_editor(RolloutStage.offline)
    assert isinstance(editor, ContainerAiderEditor)


def test_pr_stage_requires_named_filtered_sandbox_network(monkeypatch):
    monkeypatch.setenv("CODEGEN_SANDBOX", "docker")
    monkeypatch.setattr(
        ContainerAiderEditor,
        "assert_runtime_ready",
        lambda self, *, expected_revision: None,
    )
    for network in ("", "bridge", "default", "host", "none"):
        monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", network)
        with pytest.raises(RuntimeError, match="SANDBOX_NETWORK"):
            _make_editor(RolloutStage.reviewed_pr)

    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "codegen-egress-filtered")
    assert isinstance(
        _make_editor(RolloutStage.reviewed_pr), ContainerAiderEditor
    )


def test_development_pr_preflights_mutable_local_worker(monkeypatch):
    observed: dict[str, object] = {}

    def fake_preflight(
        self,
        *,
        expected_revision: str,
        require_immutable_image: bool = True,
    ) -> None:
        observed.update(
            expected_revision=expected_revision,
            require_immutable_image=require_immutable_image,
        )

    monkeypatch.setenv("CODEGEN_SANDBOX", "docker")
    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "codegen-development")
    monkeypatch.setenv("CODEGEN_REVISION", DEVELOPMENT_CODEGEN_REVISION)
    monkeypatch.setattr(ContainerAiderEditor, "assert_runtime_ready", fake_preflight)

    assert isinstance(
        _make_editor(RolloutStage.development_pr), ContainerAiderEditor
    )
    assert observed == {
        "expected_revision": DEVELOPMENT_CODEGEN_REVISION,
        "require_immutable_image": False,
    }


def test_in_process_editor_is_explicit_trusted_dev_only(monkeypatch):
    monkeypatch.setenv("CODEGEN_SANDBOX", "in-process")
    monkeypatch.delenv("CODEGEN_TRUSTED_REPOS_ONLY", raising=False)
    with pytest.raises(RuntimeError, match="TRUSTED_REPOS_ONLY"):
        _make_editor(RolloutStage.offline)

    monkeypatch.setenv("CODEGEN_TRUSTED_REPOS_ONLY", "true")
    assert isinstance(_make_editor(RolloutStage.shadow), AiderEditor)
    with pytest.raises(RuntimeError, match="require CODEGEN_SANDBOX=docker"):
        _make_editor(RolloutStage.development_pr)
    with pytest.raises(RuntimeError, match="require CODEGEN_SANDBOX=docker"):
        _make_editor(RolloutStage.low_risk_canary)
