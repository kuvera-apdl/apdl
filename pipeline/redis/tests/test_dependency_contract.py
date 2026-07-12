from pathlib import Path


REDIS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = REDIS_DIR.parents[1]


def test_fresh_setup_installs_writer_tools_without_bloating_runtime_image():
    dev_requirements = (REDIS_DIR / "requirements-dev.txt").read_text().splitlines()
    makefile = (REPO_ROOT / "Makefile").read_text()
    dockerfile = (REDIS_DIR / "Dockerfile").read_text()
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "-r requirements.txt" in dev_requirements
    assert any(line.startswith("pytest>=") for line in dev_requirements)
    assert any(line.startswith("ruff>=") for line in dev_requirements)
    assert "uv pip install -r requirements-dev.txt" in makefile
    assert "COPY requirements.txt ." in dockerfile
    assert "requirements-dev.txt" not in dockerfile
    assert "clickhouse-writer:" in workflow
    assert "pip install -r requirements-dev.txt" in workflow
    assert "ruff check clickhouse_writer.py tests/" in workflow
    assert "python -m pytest -q" in workflow
