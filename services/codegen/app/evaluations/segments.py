"""Deterministic full-metric slices for continuous evaluation reporting."""

from __future__ import annotations

import json
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from app.evaluations.metrics import aggregate_metrics, build_evaluation_report
from app.evaluations.models import (
    CaseId,
    EvaluationCorpus,
    EvaluationRun,
    EvaluationSummary,
    MetricName,
    Sha256,
    StrictModel,
    canonical_sha256,
)


class SegmentDimension(str, Enum):
    model = "model"
    ecosystem = "ecosystem"
    task_type = "task_type"
    risk = "risk"


class EvaluationSegment(StrictModel):
    schema_version: Literal["evaluation_segment@1"] = "evaluation_segment@1"
    dimension: SegmentDimension
    value: str = Field(min_length=1)
    case_ids: list[CaseId] = Field(min_length=1)
    slice_run_sha256: Sha256
    summary: EvaluationSummary

    @model_validator(mode="after")
    def validate_complete_slice_provenance(self) -> EvaluationSegment:
        if self.case_ids != sorted(self.case_ids) or len(self.case_ids) != len(
            set(self.case_ids)
        ):
            raise ValueError("segment case_ids must be unique and sorted")
        if self.summary.sample_size != len(self.case_ids):
            raise ValueError("segment sample size does not match its case ids")
        if self.summary.run_sha256 != self.slice_run_sha256:
            raise ValueError("segment summary does not match its slice run")
        expected_cases = set(self.case_ids)
        for metric_name in MetricName:
            metric = getattr(self.summary, metric_name.value)
            represented = set(metric.provenance.included_case_ids) | {
                item.case_id for item in metric.provenance.exclusions
            }
            if represented != expected_cases:
                raise ValueError(
                    f"segment metric {metric_name.value} has incomplete case provenance"
                )
        return self


class SegmentedEvaluationReport(StrictModel):
    schema_version: Literal["segmented_evaluation_report@1"] = (
        "segmented_evaluation_report@1"
    )
    run_id: str = Field(min_length=1)
    run_sha256: Sha256
    corpus_id: str = Field(min_length=1)
    corpus_sha256: Sha256
    overall_report_sha256: Sha256
    segments: list[EvaluationSegment] = Field(min_length=4)
    segmented_report_sha256: Sha256

    @model_validator(mode="after")
    def validate_content_address_and_dimensions(self) -> SegmentedEvaluationReport:
        expected_order = sorted(
            self.segments,
            key=lambda item: (
                list(SegmentDimension).index(item.dimension),
                item.value,
            ),
        )
        if self.segments != expected_order:
            raise ValueError("evaluation segments must be deterministically sorted")
        keys = [(item.dimension, item.value) for item in self.segments]
        if len(keys) != len(set(keys)):
            raise ValueError("segment dimension/value keys must be unique")
        if {item.dimension for item in self.segments} != set(SegmentDimension):
            raise ValueError("segmented reports require every canonical dimension")
        if sum(item.dimension is SegmentDimension.model for item in self.segments) != 1:
            raise ValueError("segmented reports require exactly one model segment")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"segmented_report_sha256"})
        )
        if self.segmented_report_sha256 != expected:
            raise ValueError("segmented_report_sha256 does not match report contents")
        return self


def _validate_alignment(run: EvaluationRun, corpus: EvaluationCorpus) -> None:
    if run.corpus_id != corpus.corpus_id:
        raise ValueError("evaluation run corpus_id does not match the corpus")
    if run.corpus_sha256 != corpus.evidence_sha256():
        raise ValueError("evaluation run corpus digest does not match the corpus")
    run_case_ids = {outcome.case_id for outcome in run.outcomes}
    corpus_case_ids = {case.case_id for case in corpus.cases}
    if run_case_ids != corpus_case_ids:
        raise ValueError("evaluation run outcomes must cover corpus cases exactly")
    expected_fixtures = {
        case.case_id: case.fixture_sha256 for case in corpus.cases
    }
    if run.fixture_sha256_by_case != expected_fixtures:
        raise ValueError("evaluation run fixture provenance does not match the corpus")


def build_segmented_report(
    run: EvaluationRun,
    corpus: EvaluationCorpus,
) -> SegmentedEvaluationReport:
    """Aggregate every metric for deterministic model/ecosystem/task/risk slices."""
    run = EvaluationRun.model_validate(run.model_dump(mode="python"))
    corpus = EvaluationCorpus.model_validate(corpus.model_dump(mode="python"))
    _validate_alignment(run, corpus)
    overall = build_evaluation_report(run)
    cases = {case.case_id: case for case in corpus.cases}
    outcomes = {outcome.case_id: outcome for outcome in run.outcomes}
    groups: dict[tuple[SegmentDimension, str], list[str]] = {
        (SegmentDimension.model, run.model): sorted(outcomes)
    }
    for case_id, case in cases.items():
        dimensions = (
            (SegmentDimension.ecosystem, case.ecosystem.value),
            (SegmentDimension.task_type, case.mutation.value),
            (SegmentDimension.risk, case.task.risk.value),
        )
        for key in dimensions:
            groups.setdefault(key, []).append(case_id)

    segments: list[EvaluationSegment] = []
    for (dimension, value), case_ids in sorted(
        groups.items(), key=lambda item: (list(SegmentDimension).index(item[0][0]), item[0][1])
    ):
        ordered_ids = sorted(case_ids)
        slice_run = EvaluationRun.model_validate(
            {
                **run.model_dump(mode="python", exclude={"run_id", "outcomes", "fixture_sha256_by_case"}),
                "run_id": f"{run.run_id}::segment::{dimension.value}::{value}",
                "outcomes": [outcomes[case_id].model_dump(mode="python") for case_id in ordered_ids],
                "fixture_sha256_by_case": {
                    case_id: run.fixture_sha256_by_case[case_id]
                    for case_id in ordered_ids
                },
            }
        )
        summary = aggregate_metrics(slice_run)
        segments.append(
            EvaluationSegment(
                dimension=dimension,
                value=value,
                case_ids=ordered_ids,
                slice_run_sha256=slice_run.evidence_sha256(),
                summary=summary,
            )
        )

    payload = {
        "schema_version": "segmented_evaluation_report@1",
        "run_id": run.run_id,
        "run_sha256": run.evidence_sha256(),
        "corpus_id": corpus.corpus_id,
        "corpus_sha256": corpus.evidence_sha256(),
        "overall_report_sha256": overall.report_sha256,
        "segments": [segment.model_dump(mode="json") for segment in segments],
    }
    return SegmentedEvaluationReport.model_validate_json(
        json.dumps(
            {**payload, "segmented_report_sha256": canonical_sha256(payload)},
            allow_nan=False,
            separators=(",", ":"),
        )
    )
