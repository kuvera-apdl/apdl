"""Load and cross-check public corpus data and sealed evaluator oracles."""

from __future__ import annotations

from pathlib import Path

from app.evaluations.models import (
    EvaluationCorpus,
    EvaluationOracleSet,
    EvaluationRun,
    canonical_sha256,
)

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


def validate_run_provenance(
    run: EvaluationRun,
    *,
    corpus: EvaluationCorpus | None = None,
    oracle_set: EvaluationOracleSet | None = None,
) -> None:
    """Bind a run to the exact currently trusted corpus and sealed oracles."""
    resolved_corpus = corpus or load_corpus()
    resolved_oracles = oracle_set or load_oracle_set()
    validate_corpus_oracles(resolved_corpus, resolved_oracles)
    if run.corpus_id != resolved_corpus.corpus_id:
        raise ValueError("evaluation run corpus_id is not the trusted corpus")
    if run.corpus_sha256 != resolved_corpus.evidence_sha256():
        raise ValueError("evaluation run corpus digest is stale or untrusted")
    if run.oracle_set_sha256 != resolved_oracles.evidence_sha256():
        raise ValueError("evaluation run oracle-set digest is stale or untrusted")
    case_by_id = {case.case_id: case for case in resolved_corpus.cases}
    oracle_by_id = {oracle.case_id: oracle for oracle in resolved_oracles.oracles}
    outcome_by_id = {outcome.case_id: outcome for outcome in run.outcomes}
    if set(outcome_by_id) != set(case_by_id):
        raise ValueError("evaluation run must cover every trusted corpus case exactly")
    expected_fixtures = {
        case_id: case.fixture_sha256 for case_id, case in case_by_id.items()
    }
    if run.fixture_sha256_by_case != expected_fixtures:
        raise ValueError("evaluation run fixture provenance is stale or untrusted")
    for case_id, outcome in outcome_by_id.items():
        if outcome.oracle_case_sha256 != canonical_sha256(oracle_by_id[case_id]):
            raise ValueError(
                f"evaluation outcome oracle digest is stale for case {case_id}"
            )
