"""Continuous code-generation evaluation contracts and rollout controls."""

from app.evaluations.corpus import load_corpus, load_oracle_set
from app.evaluations.execution import CompletedEvaluation, execute_evaluation_run
from app.evaluations.metrics import aggregate_metrics, build_evaluation_report
from app.evaluations.models import (
    AggregateMetric,
    EvaluationCase,
    EvaluationCorpus,
    EvaluationExecution,
    EvaluationReport,
    EvaluationRun,
    EvaluationSummary,
    RolloutStage,
)
from app.evaluations.publication import (
    PublicationAuthorization,
    PublicationAuthorizationProvider,
    PublicationEvidenceBundle,
    PublicationRequest,
    TrustedPublicationAuthorizer,
    build_publication_bundle,
    load_publication_authorizer,
    load_publication_bundle,
    load_rollout_policy,
)
from app.evaluations.segments import (
    EvaluationSegment,
    SegmentedEvaluationReport,
    SegmentDimension,
    build_segmented_report,
)
from app.evaluations.subprocess_executor import (
    PublicEvaluationInvocation,
    SubprocessEvaluationExecutor,
)
from app.evaluations.rollout import decide_rollout, in_canary_cohort

__all__ = [
    "AggregateMetric",
    "CompletedEvaluation",
    "EvaluationCase",
    "EvaluationCorpus",
    "EvaluationExecution",
    "EvaluationReport",
    "EvaluationSegment",
    "EvaluationRun",
    "EvaluationSummary",
    "PublicationAuthorization",
    "PublicationAuthorizationProvider",
    "PublicationEvidenceBundle",
    "PublicationRequest",
    "PublicEvaluationInvocation",
    "RolloutStage",
    "SegmentDimension",
    "SegmentedEvaluationReport",
    "SubprocessEvaluationExecutor",
    "TrustedPublicationAuthorizer",
    "aggregate_metrics",
    "build_evaluation_report",
    "build_publication_bundle",
    "build_segmented_report",
    "decide_rollout",
    "in_canary_cohort",
    "execute_evaluation_run",
    "load_corpus",
    "load_oracle_set",
    "load_publication_authorizer",
    "load_publication_bundle",
    "load_rollout_policy",
]
