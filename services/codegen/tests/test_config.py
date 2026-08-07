"""Tests for environment-derived Codegen configuration."""

import base64

import pytest

from app import config
from app.editor.deadlines import (
    CODEGEN_JOB_OVERHEAD_SECONDS,
    CodegenDeadlineExceeded,
    CodegenRunDeadline,
)
from app.editor.aider_editor import AiderEditor
from app.editor.container_editor import ContainerAiderEditor
from app.llm.contracts import LlmAssignmentSnapshot, LlmExecutionSnapshot
from app.llm.provider_catalog import CATALOG_VERSION
from app.main import _make_editor, _make_publication_gate
from app.models.execution import PublicationStage, RiskLevel
from app.publication import (
    DEVELOPMENT_CODEGEN_REVISION,
    DevelopmentPublicationAuthorization,
    PublicationGateError,
    TenantPublicationAuthorization,
    TenantPublicationRuntimeIdentity,
)

_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBVQIBADAN\n-----END RSA PRIVATE KEY-----\n"

_PRIVATE_KEY_SETTING = "GITHUB_APP_PRIVATE_KEY_BASE64"


def _assignment(role: str, model_id: str) -> LlmAssignmentSnapshot:
    return LlmAssignmentSnapshot(
        schema_version="codegen_llm_assignment_snapshot@1",
        role=role,
        provider="openai",
        model_id=model_id,
        assignment_version=1,
        connection_version=1,
        inventory_version=1,
        catalog_version=CATALOG_VERSION,
        context_window_tokens=400_000,
        supports_tool_calling=True,
        supports_structured_output=True,
        input_cost_per_million_tokens_usd_micros=750_000,
        output_cost_per_million_tokens_usd_micros=4_500_000,
    )


def _execution_snapshot(
    *,
    stage: PublicationStage,
    revision: str,
    behavior_configuration_sha256: str,
) -> LlmExecutionSnapshot:
    return LlmExecutionSnapshot(
        schema_version="codegen_llm_execution_snapshot@2",
        project_id="demo",
        repository_grant_id="ghg_demo",
        repository_id=1,
        repository_installation_id=2,
        repository_full_name="acme/widgets",
        codegen_revision=revision,
        behavior_configuration_sha256=behavior_configuration_sha256,
        rollout_stage=stage.value,
        assignments=(
            _assignment("editor", "gpt-5.4-mini"),
            _assignment("helper", "gpt-5.4-nano"),
        ),
    )


def _configure_tenant_publication(
    monkeypatch: pytest.MonkeyPatch,
    *,
    controller_image: str = "sha256:" + "a" * 64,
    worker_image: str = "sha256:" + "b" * 64,
    max_concurrent_jobs: int = 1,
) -> None:
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "tenant_draft_pr")
    monkeypatch.setenv("CODEGEN_REVISION", "tenant-revision")
    monkeypatch.setenv("CODEGEN_CONTROLLER_IMAGE_ID", controller_image)
    monkeypatch.setenv("CODEGEN_SANDBOX_IMAGE", worker_image)
    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "d" * 64)
    monkeypatch.setenv(
        "CODEGEN_EGRESS_PROXY_IMAGE_ID",
        "sha256:" + "c" * 64,
    )
    monkeypatch.setenv("CODEGEN_EGRESS_SOCKET_VOLUME", "apdl-tenant-egress")
    monkeypatch.setenv(
        "CODEGEN_MAX_CONCURRENT_JOBS",
        str(max_concurrent_jobs),
    )


def test_base64_key_is_decoded(monkeypatch):
    monkeypatch.setenv(
        _PRIVATE_KEY_SETTING,
        base64.b64encode(_PEM.encode()).decode(),
    )
    assert config.github_app_private_key() == _PEM


def test_empty_when_nothing_set(monkeypatch):
    monkeypatch.delenv(_PRIVATE_KEY_SETTING, raising=False)
    assert config.github_app_private_key() == ""


@pytest.mark.parametrize(
    "encoded",
    [
        "not%%%base64%%%",
        "TQ",
        "TQ===",
        "_w==",
    ],
    ids=["alphabet", "padding-missing", "padding-extra", "url-safe"],
)
def test_noncanonical_base64_fails_closed(monkeypatch, caplog, encoded):
    monkeypatch.setenv(_PRIVATE_KEY_SETTING, encoded)

    assert config.github_app_private_key() == ""
    assert _PRIVATE_KEY_SETTING in caplog.text
    assert encoded not in caplog.text


def test_wrapped_base64_fails_with_canonical_generation_guidance(
    monkeypatch,
    caplog,
):
    encoded = "Zm9v\nYmFy"
    monkeypatch.setenv(_PRIVATE_KEY_SETTING, encoded)

    assert config.github_app_private_key() == ""
    assert "single unwrapped Base64 line" in caplog.text
    assert "openssl base64 -A" in caplog.text
    assert encoded not in caplog.text


def test_non_utf8_private_key_fails_closed(monkeypatch, caplog):
    encoded = base64.b64encode(b"\xff\xfe").decode()
    monkeypatch.setenv(_PRIVATE_KEY_SETTING, encoded)

    assert config.github_app_private_key() == ""
    assert _PRIVATE_KEY_SETTING in caplog.text
    assert encoded not in caplog.text


def test_empty_private_key_material_fails_closed(monkeypatch, caplog):
    encoded = base64.b64encode(b" \n\t").decode()
    monkeypatch.setenv(_PRIVATE_KEY_SETTING, encoded)

    assert config.github_app_private_key() == ""
    assert _PRIVATE_KEY_SETTING in caplog.text
    assert encoded not in caplog.text


def test_github_origins_are_fixed_to_github_dot_com(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://attacker.example")
    monkeypatch.setenv("GITHUB_WEB_URL", "https://attacker.example")

    assert config.GITHUB_API_URL == "https://api.github.com"
    assert config.GITHUB_WEB_URL == "https://github.com"


@pytest.mark.parametrize("length", [32, 128])
@pytest.mark.parametrize("poll_interval", [0, 60])
def test_github_webhook_secret_accepts_canonical_boundaries(
    monkeypatch,
    length,
    poll_interval,
):
    secret = "a" * length
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

    assert (
        config.github_webhook_secret(ci_poll_interval=poll_interval) == secret
    )


@pytest.mark.parametrize("configured", [False, True], ids=["unset", "empty"])
def test_github_webhook_secret_may_be_blank_while_polling(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    if configured:
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    else:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)

    assert config.github_webhook_secret(ci_poll_interval=60) is None


@pytest.mark.parametrize("configured", [False, True], ids=["unset", "empty"])
def test_github_webhook_secret_is_required_when_polling_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    if configured:
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "")
    else:
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)

    with pytest.raises(
        RuntimeError,
        match="required when CODEGEN_CI_POLL_INTERVAL is 0",
    ):
        config.github_webhook_secret(ci_poll_interval=0)


@pytest.mark.parametrize(
    "secret",
    [
        "a" * 31,
        "a" * 129,
        " leading" + "a" * 32,
        "a" * 32 + " ",
        "a" * 16 + "." + "b" * 16,
        "a" * 31 + "é",
    ],
    ids=[
        "too-short",
        "too-long",
        "leading-space",
        "trailing-space",
        "punctuation",
        "non-ascii",
    ],
)
@pytest.mark.parametrize("poll_interval", [0, 60])
def test_github_webhook_secret_rejects_noncanonical_values(
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    poll_interval: int,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        config.github_webhook_secret(ci_poll_interval=poll_interval)


@pytest.mark.parametrize(
    "value",
    [
        "//tmp/apdl-codegen-llm-broker",
        "/tmp//apdl-codegen-llm-broker",
    ],
)
def test_llm_broker_directory_rejects_noncanonical_slashes(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("CODEGEN_LLM_BROKER_DIR", value)

    with pytest.raises(ValueError, match="canonical safe absolute path"):
        config.codegen_llm_broker_dir()


def test_cors_origins_default_to_local_admin(monkeypatch):
    monkeypatch.delenv("CODEGEN_CORS_ORIGINS", raising=False)
    origins = config.codegen_cors_origins()
    assert "http://localhost:5174" in origins
    assert "*" not in origins  # never wildcard — this service merges PRs


def test_cors_origins_parsed_from_env(monkeypatch):
    monkeypatch.setenv(
        "CODEGEN_CORS_ORIGINS", "https://admin.example.com, https://ops.example.com "
    )
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


def test_job_budget_counts_brief_and_every_possible_review(monkeypatch):
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    monkeypatch.setenv("CODEGEN_TIMEOUT", "100")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "10")
    monkeypatch.setenv("CODEGEN_LLM_TIMEOUT", "20")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "2")
    monkeypatch.setenv("CODEGEN_BRIEF", "true")
    monkeypatch.setenv("CODEGEN_REVIEW", "true")

    plan = config.codegen_deadline_plan()

    assert plan.edit_rounds == 3
    assert plan.brief_calls == 1
    assert plan.review_calls == 3
    assert plan.requested_phase_seconds == 400
    assert plan.job_budget_seconds == CODEGEN_JOB_OVERHEAD_SECONDS + 400
    assert plan.reconciled is False


def test_inner_deadlines_are_reconciled_with_credential_safe_outer_cap(monkeypatch):
    monkeypatch.delenv("CODEGEN_JOB_BUDGET", raising=False)
    monkeypatch.setenv("CODEGEN_TIMEOUT", "1800")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "300")
    monkeypatch.setenv("CODEGEN_LLM_TIMEOUT", "240")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    monkeypatch.setenv("CODEGEN_BRIEF", "true")
    monkeypatch.setenv("CODEGEN_REVIEW", "true")

    plan = config.codegen_deadline_plan()

    assert plan.job_budget_seconds == config.MAX_CODEGEN_JOB_BUDGET_SECONDS
    assert plan.reconciled is True
    assert plan.reserved_seconds <= plan.job_budget_seconds
    assert config.codegen_agent_timeout() == plan.agent_timeout_seconds
    assert config.codegen_git_timeout() == plan.git_timeout_seconds
    assert config.codegen_llm_timeout() == plan.llm_timeout_seconds


def test_job_budget_env_override_can_tighten_but_not_expand_token_bound(monkeypatch):
    monkeypatch.setenv("CODEGEN_JOB_BUDGET", "2400")
    assert config.codegen_job_budget() == 2400

    monkeypatch.setenv("CODEGEN_JOB_BUDGET", "7200")
    with pytest.raises(ValueError, match="cannot exceed 3000 seconds"):
        config.codegen_job_budget()


def test_tight_job_override_scales_every_active_inner_deadline(monkeypatch):
    monkeypatch.setenv("CODEGEN_JOB_BUDGET", "200")
    monkeypatch.setenv("CODEGEN_TIMEOUT", "100")
    monkeypatch.setenv("CODEGEN_GIT_TIMEOUT", "20")
    monkeypatch.setenv("CODEGEN_LLM_TIMEOUT", "10")
    monkeypatch.setenv("CODEGEN_EDIT_RETRIES", "1")
    monkeypatch.setenv("CODEGEN_BRIEF", "true")
    monkeypatch.setenv("CODEGEN_REVIEW", "true")

    plan = config.codegen_deadline_plan()

    assert plan.job_budget_seconds == 200
    assert plan.reconciled is True
    assert plan.agent_timeout_seconds < 100
    assert plan.git_timeout_seconds < 20
    assert plan.llm_timeout_seconds < 10
    assert plan.reserved_seconds <= plan.job_budget_seconds


def test_run_deadline_clamps_every_phase_to_shared_remaining_wall_time():
    now = [100.0]
    deadline = CodegenRunDeadline(100, clock=lambda: now[0])

    assert deadline.remaining_seconds() == 40
    assert deadline.clamp_timeout(300) == 40

    now[0] += 39.75
    assert deadline.clamp_timeout(10) == pytest.approx(0.25)

    now[0] += 0.25
    with pytest.raises(CodegenDeadlineExceeded):
        deadline.clamp_timeout(1)


def test_stale_sweep_interval_default_and_disable(monkeypatch):
    monkeypatch.delenv("CODEGEN_STALE_SWEEP_INTERVAL", raising=False)
    assert config.codegen_stale_sweep_interval() == 300
    monkeypatch.setenv("CODEGEN_STALE_SWEEP_INTERVAL", "0")
    assert config.codegen_stale_sweep_interval() == 0


def test_rollout_config_defaults_fail_closed_and_binds_revision(monkeypatch):
    monkeypatch.delenv("CODEGEN_ROLLOUT_STAGE", raising=False)
    monkeypatch.delenv("CODEGEN_REVISION", raising=False)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)

    assert config.codegen_rollout_stage() is PublicationStage.offline
    assert config.codegen_revision() == "development-unversioned"

    monkeypatch.setenv("CODEGEN_REVISION", "image@sha256:abc")
    assert config.codegen_revision() == "image@sha256:abc"


@pytest.mark.parametrize(
    "stage",
    ["automatic_merge", "shadow", "reviewed_pr", "low_risk_canary"],
)
def test_rollout_stage_rejects_unknown_or_retired_values(monkeypatch, stage):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", stage)
    with pytest.raises(ValueError, match="CODEGEN_ROLLOUT_STAGE"):
        config.codegen_rollout_stage()


def test_development_mode_requires_explicit_true(monkeypatch):
    monkeypatch.delenv("CODEGEN_DEVELOPMENT_MODE", raising=False)
    assert config.codegen_development_mode() is False
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    assert config.codegen_development_mode() is True
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "false")
    assert config.codegen_development_mode() is False


def test_development_publication_gate_is_explicit_and_draft_only(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "development_pr")
    monkeypatch.setenv("CODEGEN_REVISION", DEVELOPMENT_CODEGEN_REVISION)
    monkeypatch.delenv("CODEGEN_DEVELOPMENT_MODE", raising=False)

    with pytest.raises(RuntimeError, match="CODEGEN_DEVELOPMENT_MODE=true"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    gate = _make_publication_gate()
    snapshot = _execution_snapshot(
        stage=PublicationStage.development_pr,
        revision=DEVELOPMENT_CODEGEN_REVISION,
        behavior_configuration_sha256=gate.behavior_configuration_sha256,
    )
    authorization = gate.authorize(
        risk=RiskLevel.low,
        snapshot=snapshot,
    )

    assert gate.stage is PublicationStage.development_pr
    assert isinstance(authorization, DevelopmentPublicationAuthorization)
    assert authorization.request.codegen_revision == DEVELOPMENT_CODEGEN_REVISION
    assert authorization.request.model == "openai/gpt-5.4-mini"
    assert authorization.decision.ready_for_review is False
    assert authorization.draft_only is True
    assert "report_sha256" not in authorization.model_dump(mode="json")


def test_development_publication_rejects_non_dev_revision(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "development_pr")
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    monkeypatch.setenv("CODEGEN_REVISION", "production-revision")
    with pytest.raises(ValueError, match="local-development"):
        _make_publication_gate()


def test_development_marker_is_rejected_for_offline_stage(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "offline")
    monkeypatch.setenv("CODEGEN_DEVELOPMENT_MODE", "true")
    with pytest.raises(RuntimeError, match="valid only with development_pr"):
        _make_publication_gate()


def test_tenant_publication_requires_attested_runtime_contract(monkeypatch):
    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "tenant_draft_pr")
    monkeypatch.setenv("CODEGEN_REVISION", "tenant-revision")
    monkeypatch.delenv("CODEGEN_EGRESS_POLICY_SHA256", raising=False)
    monkeypatch.delenv("CODEGEN_EGRESS_PROXY_IMAGE_ID", raising=False)
    monkeypatch.delenv("CODEGEN_EGRESS_SOCKET_VOLUME", raising=False)

    with pytest.raises(RuntimeError, match="EGRESS_POLICY_SHA256"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_EGRESS_POLICY_SHA256", "d" * 64)
    with pytest.raises(RuntimeError, match="EGRESS_PROXY_IMAGE_ID"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_EGRESS_PROXY_IMAGE_ID", "sha256:" + "c" * 64)
    with pytest.raises(RuntimeError, match="EGRESS_SOCKET_VOLUME"):
        _make_publication_gate()

    monkeypatch.setenv("CODEGEN_ROLLOUT_STAGE", "offline")
    gate = _make_publication_gate()
    assert gate.stage is PublicationStage.offline
    assert gate.runtime_identity is None


def test_publication_gate_binds_exact_runtime_and_tenant_assignments(monkeypatch):
    _configure_tenant_publication(monkeypatch)
    gate = _make_publication_gate()
    identity = TenantPublicationRuntimeIdentity.build(
        controller_image_id="sha256:" + "a" * 64,
        worker_image_id="sha256:" + "b" * 64,
        codegen_revision="tenant-revision",
        behavior_configuration_sha256=gate.behavior_configuration_sha256,
        egress_policy_sha256="d" * 64,
        egress_proxy_image_id="sha256:" + "c" * 64,
        max_concurrent_jobs=1,
    )
    snapshot = _execution_snapshot(
        stage=PublicationStage.tenant_draft_pr,
        revision="tenant-revision",
        behavior_configuration_sha256=gate.behavior_configuration_sha256,
    )
    authorization = gate.authorize(risk=RiskLevel.medium, snapshot=snapshot)

    assert gate.stage is PublicationStage.tenant_draft_pr
    assert gate.runtime_identity == identity
    assert isinstance(authorization, TenantPublicationAuthorization)
    assert authorization.authority == "tenant_model_assignments"
    assert authorization.request.execution_snapshot == snapshot
    assert authorization.decision.ready_for_review is False
    assert authorization.draft_only is True


def test_tenant_publication_gate_rejects_snapshot_runtime_drift(monkeypatch) -> None:
    _configure_tenant_publication(monkeypatch)
    gate = _make_publication_gate()
    snapshot = _execution_snapshot(
        stage=PublicationStage.tenant_draft_pr,
        revision="tenant-revision",
        behavior_configuration_sha256="e" * 64,
    )
    with pytest.raises(
        PublicationGateError,
        match="configured behavior",
    ):
        gate.authorize(risk=RiskLevel.low, snapshot=snapshot)


def test_publication_gate_rejects_mutable_image_identity(monkeypatch):
    _configure_tenant_publication(
        monkeypatch,
        controller_image="controller:latest",
        worker_image="worker:latest",
    )

    with pytest.raises(ValueError, match="immutable sha256"):
        _make_publication_gate()


def test_publication_gate_rejects_tenant_concurrency_above_one(monkeypatch):
    _configure_tenant_publication(monkeypatch, max_concurrent_jobs=2)

    with pytest.raises(RuntimeError, match="MAX_CONCURRENT_JOBS=1"):
        _make_publication_gate()


def test_editor_defaults_to_isolated_container(monkeypatch):
    monkeypatch.delenv("CODEGEN_SANDBOX", raising=False)
    editor = _make_editor(PublicationStage.offline)
    assert isinstance(editor, ContainerAiderEditor)


def test_tenant_draft_pr_stage_requires_network_none_workers(monkeypatch):
    monkeypatch.setenv("CODEGEN_SANDBOX", "docker")
    monkeypatch.setattr(
        ContainerAiderEditor,
        "assert_runtime_ready",
        lambda self, *, expected_revision: None,
    )
    for network in ("bridge", "default", "host", "none", "custom"):
        monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", network)
        with pytest.raises(RuntimeError, match="SANDBOX_NETWORK"):
            _make_editor(PublicationStage.tenant_draft_pr)

    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "")
    assert isinstance(
        _make_editor(PublicationStage.tenant_draft_pr),
        ContainerAiderEditor,
    )


def test_development_pr_preflights_mutable_local_worker(monkeypatch):
    observed: dict[str, object] = {}

    def fake_preflight(
        self,
        *,
        expected_revision: str,
        require_immutable_image: bool = True,
        require_egress_policy: bool = True,
    ) -> None:
        observed.update(
            expected_revision=expected_revision,
            require_immutable_image=require_immutable_image,
            require_egress_policy=require_egress_policy,
        )

    monkeypatch.setenv("CODEGEN_SANDBOX", "docker")
    monkeypatch.setenv("CODEGEN_SANDBOX_NETWORK", "codegen-development")
    monkeypatch.setenv("CODEGEN_REVISION", DEVELOPMENT_CODEGEN_REVISION)
    monkeypatch.setattr(ContainerAiderEditor, "assert_runtime_ready", fake_preflight)

    assert isinstance(
        _make_editor(PublicationStage.development_pr),
        ContainerAiderEditor,
    )
    assert observed == {
        "expected_revision": DEVELOPMENT_CODEGEN_REVISION,
        "require_immutable_image": False,
        "require_egress_policy": False,
    }


def test_in_process_editor_is_explicit_trusted_dev_only(monkeypatch):
    monkeypatch.setenv("CODEGEN_SANDBOX", "in-process")
    monkeypatch.delenv("CODEGEN_TRUSTED_REPOS_ONLY", raising=False)
    with pytest.raises(RuntimeError, match="TRUSTED_REPOS_ONLY"):
        _make_editor(PublicationStage.offline)

    monkeypatch.setenv("CODEGEN_TRUSTED_REPOS_ONLY", "true")
    assert isinstance(_make_editor(PublicationStage.offline), AiderEditor)
    with pytest.raises(RuntimeError, match="require CODEGEN_SANDBOX=docker"):
        _make_editor(PublicationStage.development_pr)
    with pytest.raises(RuntimeError, match="require CODEGEN_SANDBOX=docker"):
        _make_editor(PublicationStage.tenant_draft_pr)
