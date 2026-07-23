from pathlib import Path


AGENTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENTS_DIR.parents[1]
PYTHON_IMAGE = (
    "python:3.12-slim@sha256:"
    "423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf"
)
MODEL_REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"


def test_runtime_image_uses_immutable_dependencies_and_model() -> None:
    dockerfile = (AGENTS_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith(f"FROM {PYTHON_IMAGE}\n")
    assert "COPY requirements.lock ." in dockerfile
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock" in dockerfile
    assert "COPY pyproject.toml ." not in dockerfile
    assert "qdrant/bge-small-en-v1.5-onnx-q" in dockerfile
    assert f"EMBEDDING_MODEL_REVISION={MODEL_REVISION}" in dockerfile
    assert "specific_model_path=os.environ['EMBEDDING_MODEL_PATH']" in dockerfile
    assert "local_files_only=True" in dockerfile
    assert "ENV HF_HUB_OFFLINE=1" in dockerfile


def test_published_locks_are_committed_dependency_audit_inputs() -> None:
    audit_script = (REPO_ROOT / "scripts" / "audit_dependencies.sh").read_text(
        encoding="utf-8"
    )
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    for source in (audit_script, workflow):
        assert "services/agents/requirements.lock" in source
        assert "services/codegen/requirements.lock" in source
        assert "services/codegen/scripts/audit_worker_dependencies.sh" in source
        assert "sdk/python services/agents services/codegen" not in source

    assert "services/codegen/requirements-agent.lock" in audit_script
    assert "for project in sdk/python services/codegen" not in audit_script
    assert "for project in sdk/python services/codegen" not in workflow


def test_compose_healthcheck_uses_core_readiness_only() -> None:
    compose = (REPO_ROOT / "infra" / "docker" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    agents_service = compose.split("\n  agents:\n", 1)[1].split(
        "\n  # Codegen", 1
    )[0]

    assert "http://localhost:8083/ready" in agents_service
    assert "['status'] == 'ready'" in agents_service
    assert "/ready/capabilities" not in agents_service
