from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_DOCKERFILE = REPO_ROOT / "services/codegen/Dockerfile"


def test_controller_source_is_readable_by_an_overridden_runtime_uid() -> None:
    source = CONTROLLER_DOCKERFILE.read_text(encoding="utf-8")

    copy_index = source.index("COPY app/ app/")
    permission_index = source.index("chmod -R u=rwX,go=rX /app")
    user_index = source.index("USER appuser")

    assert copy_index < permission_index < user_index
