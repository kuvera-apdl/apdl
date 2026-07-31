"""CI must exercise Codegen LLM authority against canonical PostgreSQL."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CI = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")


def test_codegen_job_runs_unit_and_live_postgres_suites_separately() -> None:
    job = CI.split("  codegen-service:", 1)[1].split(
        "  clickhouse-writer:", 1
    )[0]

    assert "services:\n      postgres:" in job
    assert (
        "pgvector/pgvector:pg16@sha256:"
        "1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
        in job
    )
    assert (
        "CODEGEN_TEST_POSTGRES_URL: "
        "postgresql://apdl:apdl_dev@localhost:5432/apdl"
        in job
    )
    assert "infra/docker/postgres/init-apdl-roles.sh" in job
    assert "docker build --tag apdl-postgres-migrate-codegen-ci" in job
    unit = (
        ".venv/bin/python -m pytest -q "
        "--ignore=tests/test_project_llm_postgres.py"
    )
    live = (
        ".venv/bin/python -m pytest -q "
        "tests/test_project_llm_postgres.py"
    )
    assert unit in job
    assert live in job
    assert ".venv/bin/ruff check app/ tests/ scripts/" in job
    assert job.index(unit) < job.index(live)


def test_local_codegen_lint_matches_ci_scope() -> None:
    lint_target = MAKEFILE.split("lint-codegen:", 1)[1].split(
        "\n\n",
        1,
    )[0]
    assert ".venv/bin/ruff check app/ tests/ scripts/" in lint_target
