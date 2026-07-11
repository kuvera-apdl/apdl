"""Concrete offline/shadow evaluation-run assembly."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import model_validator

from app.evaluations.corpus import load_oracle_set, validate_corpus_oracles
from app.evaluations.metrics import build_evaluation_report
from app.evaluations.models import (
    EvaluationCorpus,
    EvaluationReport,
    EvaluationRun,
    RolloutStage,
    Sha256,
    StrictModel,
    canonical_sha256,
)
from app.evaluations.runner import EvaluationExecutor, run_corpus
from app.evaluations.segments import (
    SegmentedEvaluationReport,
    build_segmented_report,
)


class CompletedEvaluation(StrictModel):
    schema_version: Literal["completed_evaluation@1"] = "completed_evaluation@1"
    run: EvaluationRun
    report: EvaluationReport
    segmented_report: SegmentedEvaluationReport
    completed_evaluation_sha256: Sha256

    @model_validator(mode="after")
    def validate_artifact_bindings(self) -> CompletedEvaluation:
        if self.report.run != self.run:
            raise ValueError("evaluation report does not contain the completed run")
        if self.segmented_report.run_sha256 != self.run.evidence_sha256():
            raise ValueError("segmented report does not bind the completed run")
        if self.segmented_report.overall_report_sha256 != self.report.report_sha256:
            raise ValueError("segmented report does not bind the overall report")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"completed_evaluation_sha256"})
        )
        if self.completed_evaluation_sha256 != expected:
            raise ValueError("completed evaluation content address does not match")
        return self


async def execute_evaluation_run(
    corpus: EvaluationCorpus,
    *,
    stage: RolloutStage,
    executor: EvaluationExecutor,
    model: str,
    codegen_revision: str,
    run_id: str | None = None,
) -> CompletedEvaluation:
    """Execute the entire canonical corpus and build overall plus sliced reports."""
    if not model or model != model.strip():
        raise ValueError("evaluation model must be a normalized non-empty identifier")
    if not codegen_revision or codegen_revision != codegen_revision.strip():
        raise ValueError("codegen revision must be a normalized non-empty identifier")
    oracle_set = load_oracle_set()
    validate_corpus_oracles(corpus, oracle_set)
    started_at = datetime.now(UTC)
    outcomes = await run_corpus(corpus, stage=stage, executor=executor)
    finished_at = datetime.now(UTC)
    run = EvaluationRun(
        run_id=run_id or f"evaluation-{uuid.uuid4().hex}",
        corpus_id=corpus.corpus_id,
        corpus_sha256=corpus.evidence_sha256(),
        oracle_set_sha256=oracle_set.evidence_sha256(),
        fixture_sha256_by_case={
            case.case_id: case.fixture_sha256 for case in corpus.cases
        },
        stage=stage,
        model=model,
        codegen_revision=codegen_revision,
        started_at=started_at,
        finished_at=finished_at,
        outcomes=outcomes,
    )
    report = build_evaluation_report(run)
    segmented_report = build_segmented_report(run, corpus)
    payload = {
        "schema_version": "completed_evaluation@1",
        "run": run.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "segmented_report": segmented_report.model_dump(mode="json"),
    }
    return CompletedEvaluation.model_validate(
        {**payload, "completed_evaluation_sha256": canonical_sha256(payload)}
    )
