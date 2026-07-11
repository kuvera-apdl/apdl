"""Load and cross-check public corpus data and sealed evaluator oracles."""

from __future__ import annotations

from pathlib import Path

from app.evaluations.models import EvaluationCorpus, EvaluationOracleSet

EVALUATION_ROOT = Path(__file__).parent
DEFAULT_CORPUS_PATH = EVALUATION_ROOT / "corpus_v2.json"
DEFAULT_ORACLE_PATH = EVALUATION_ROOT / "oracles_v1.json"
DEFAULT_FIXTURE_ROOT = EVALUATION_ROOT


def load_corpus(path: Path | None = None) -> EvaluationCorpus:
    source = path or DEFAULT_CORPUS_PATH
    return EvaluationCorpus.model_validate_json(source.read_text(encoding="utf-8"))


def load_oracle_set(path: Path | None = None) -> EvaluationOracleSet:
    """Load evaluator-only expectations; never attach this object to an invocation."""
    source = path or DEFAULT_ORACLE_PATH
    return EvaluationOracleSet.model_validate_json(source.read_text(encoding="utf-8"))


def validate_corpus_oracles(
    corpus: EvaluationCorpus,
    oracle_set: EvaluationOracleSet,
) -> None:
    if oracle_set.corpus_id != corpus.corpus_id:
        raise ValueError("oracle corpus_id does not match the public corpus")
    cases = {case.case_id: case for case in corpus.cases}
    oracles = {oracle.case_id: oracle for oracle in oracle_set.oracles}
    if set(cases) != set(oracles):
        raise ValueError("sealed oracles must cover public corpus cases exactly")
    for case_id, case in cases.items():
        if oracles[case_id].fixture_sha256 != case.fixture_sha256:
            raise ValueError(f"oracle fixture digest does not match case {case_id}")
