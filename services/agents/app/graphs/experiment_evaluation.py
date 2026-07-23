"""Evidence-only summaries of immutable, pipeline-complete experiment snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.framework import AgentContext, BaseAgent, register_agent
from app.llm.prompts.evaluation import (
    EXPERIMENT_EVALUATION_PROMPT,
    EXPERIMENT_EVALUATION_SYSTEM,
)
from app.tools.experiments import get_active_experiments, get_experiment_results


_RESOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_EXPERIMENTS_PER_RUN = 20
_MAX_CONCURRENT_QUERIES = 4
_PROHIBITED_DECISION_LANGUAGE = re.compile(
    r"\b(?:adopt|deploy(?:ment)?|extend|iterate|launch|loser|promote|"
    r"recommend(?:ation|ed|s)?|revert|roll[ -]?back|rollout|ship(?:ping)?|"
    r"should|stop|traffic change|winner|winning)\b",
    re.IGNORECASE,
)
_CONFIG_EXPERIMENT_FIELDS = frozenset(
    {
        "key",
        "flag_key",
        "status",
        "description",
        "default_variant",
        "traffic_percentage",
        "variants",
        "targeting_rules",
        "primary_metric",
        "statistical_plan",
        "start_date",
        "end_date",
        "version",
        "created_at",
        "updated_at",
        "archived_at",
        "archived_by",
    }
)


class _StrictFiniteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ExperimentArmEvidence(_StrictFiniteModel):
    variant: str = Field(min_length=1, max_length=128)
    sample_size: int = Field(ge=0)
    conversions: int = Field(ge=0)
    conversion_rate: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_counts(self) -> "ExperimentArmEvidence":
        if self.conversions > self.sample_size:
            raise ValueError("experiment conversions cannot exceed sample size")
        return self


class ExperimentStatisticalPlanEvidence(_StrictFiniteModel):
    protocol: Literal["fixed_horizon_fisher_newcombe_cc_plan_v1"]
    baseline_conversion_rate: float = Field(ge=0.0, le=1.0, strict=True)
    minimum_detectable_effect: float = Field(ge=1e-6, le=1.0, strict=True)
    significance_level: float = Field(ge=1e-6, le=0.5, strict=True)
    nominal_power: float = Field(gt=0.5, le=0.9999, strict=True)
    required_sample_size_per_arm: int = Field(
        ge=2,
        le=10_000_000,
        strict=True,
    )
    data_settlement_seconds: int = Field(ge=1, le=86_400, strict=True)


class ExperimentComparisonEvidence(_StrictFiniteModel):
    control_variant: str = Field(min_length=1, max_length=128)
    treatment_variant: str = Field(min_length=1, max_length=128)
    control_rate: float = Field(ge=0.0, le=1.0)
    treatment_rate: float = Field(ge=0.0, le=1.0)
    rate_difference: float = Field(ge=-1.0, le=1.0)
    confidence_interval: tuple[float, float]
    raw_p_value: float = Field(ge=0.0, le=1.0)
    adjusted_p_value: float = Field(ge=0.0, le=1.0)
    is_statistically_significant: bool

    @model_validator(mode="after")
    def validate_comparison(self) -> "ExperimentComparisonEvidence":
        lower, upper = self.confidence_interval
        if lower > upper:
            raise ValueError("experiment confidence interval is reversed")
        if self.control_variant == self.treatment_variant:
            raise ValueError("experiment comparison must use distinct variants")
        return self


class _ExperimentAnalysisBase(_StrictFiniteModel):
    experiment_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    flag_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    experiment_status: Literal["scheduled", "running", "completed", "stopped"]
    control_variant: str = Field(min_length=1, max_length=128)
    metric_event: str = Field(min_length=1, max_length=200)
    metric_direction: Literal["increase", "decrease"]
    statistical_plan: ExperimentStatisticalPlanEvidence
    start_date: AwareDatetime
    end_date: AwareDatetime
    config_version: int = Field(ge=1, strict=True)
    arms: list[ExperimentArmEvidence] = Field(min_length=2, max_length=10)
    crossover_actors: int = Field(ge=0)
    unknown_variant_actors: int = Field(ge=0)
    identity_conflict_actors: int = Field(ge=0)
    identity_quality: Literal["degraded", "unambiguous"]
    deployment_readiness: Literal["not_assessed"]

    @model_validator(mode="after")
    def validate_analysis_identity(self) -> "_ExperimentAnalysisBase":
        variants = [arm.variant for arm in self.arms]
        if len(set(variants)) != len(variants):
            raise ValueError("experiment analysis arms must be unique")
        if self.control_variant not in variants:
            raise ValueError("experiment control variant is absent from analysis arms")
        if self.end_date <= self.start_date:
            raise ValueError("experiment analysis window is invalid")
        if self.identity_quality == "unambiguous" and self.identity_conflict_actors:
            raise ValueError("unambiguous identity evidence cannot contain conflicts")
        return self


class VerifiedExperimentSnapshot(_ExperimentAnalysisBase):
    analysis_status: Literal["decision_snapshot"]
    data_completeness: Literal["verified"]
    experiment_status: Literal["completed"]
    unknown_variant_actors: Literal[0]
    inference_method: Literal["fisher_exact_two_sided"]
    interval_method: Literal["newcombe_wilson"]
    correction: Literal["bonferroni"]
    comparisons: list[ExperimentComparisonEvidence] = Field(min_length=1, max_length=9)

    @model_validator(mode="after")
    def validate_comparisons(self) -> "VerifiedExperimentSnapshot":
        variants = {arm.variant for arm in self.arms}
        treatments = set()
        for comparison in self.comparisons:
            if comparison.control_variant != self.control_variant:
                raise ValueError("comparison control does not match the experiment")
            if comparison.treatment_variant not in variants:
                raise ValueError("comparison treatment is not a declared arm")
            treatments.add(comparison.treatment_variant)
        if treatments != variants - {self.control_variant}:
            raise ValueError("experiment comparisons do not cover every treatment once")
        if len(treatments) != len(self.comparisons):
            raise ValueError("experiment comparisons contain duplicate treatments")
        return self


class NonFinalExperimentAnalysis(_ExperimentAnalysisBase):
    analysis_status: Literal["non_final"]
    data_completeness: Literal["not_verified"]
    reason: Literal[
        "experiment_not_started",
        "experiment_window_open",
        "awaiting_data_settlement",
        "experiment_running",
        "experiment_stopped",
        "no_exposures",
        "underpowered_arms",
        "non_finite_statistics",
        "identity_alias_conflicts",
        "unknown_variant_exposures",
        "data_completeness_unverified",
        "awaiting_pipeline_boundary",
        "pipeline_degraded",
        "pipeline_provenance_unavailable",
    ]
    underpowered_variants: list[str] = Field(max_length=10)


class ExperimentEvidenceNarrative(_StrictFiniteModel):
    experiment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    source_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_summary: str = Field(min_length=1, max_length=4_000)
    limitations: list[str] = Field(min_length=1, max_length=10)
    deployment_readiness: Literal["not_assessed"]

    @field_validator("limitations")
    @classmethod
    def validate_limitations(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 1_000 for value in values):
            raise ValueError("experiment limitations must be bounded non-empty text")
        if len(set(values)) != len(values):
            raise ValueError("experiment limitations must be unique")
        return values

    @model_validator(mode="after")
    def reject_decision_language(self) -> "ExperimentEvidenceNarrative":
        text = "\n".join((self.evidence_summary, *self.limitations))
        if _PROHIBITED_DECISION_LANGUAGE.search(text):
            raise ValueError(
                "experiment evidence narrative contains product-decision language"
            )
        return self


class ExperimentEvidenceSummary(ExperimentEvidenceNarrative):
    schema_version: Literal["experiment_evidence_summary@1"] = (
        "experiment_evidence_summary@1"
    )
    analysis_status: Literal["decision_snapshot"] = "decision_snapshot"
    data_completeness: Literal["verified"] = "verified"
    source_snapshot: VerifiedExperimentSnapshot


def _snapshot_sha256(snapshot: VerifiedExperimentSnapshot) -> str:
    encoded = json.dumps(
        snapshot.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _completed_experiment_keys(experiments: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for raw in experiments:
        if not isinstance(raw, dict) or set(raw) != _CONFIG_EXPERIMENT_FIELDS:
            raise ValueError(
                "Config experiment entries must use the canonical list schema"
            )
        key = raw.get("key")
        status = raw.get("status")
        if not isinstance(key, str) or _RESOURCE_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("Config experiment key is not canonical")
        if status not in {
            "draft",
            "scheduled",
            "running",
            "completed",
            "stopped",
        }:
            raise ValueError("Config experiment status is not canonical")
        if status == "completed":
            keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("Config experiment list contains duplicate keys")
    return sorted(keys)


def _analysis_result(
    value: Any,
) -> VerifiedExperimentSnapshot | NonFinalExperimentAnalysis:
    if not isinstance(value, dict):
        raise ValueError("Query experiment analysis must be an object")
    status = value.get("analysis_status")
    try:
        if status == "decision_snapshot":
            return VerifiedExperimentSnapshot.model_validate(value)
        if status == "non_final":
            return NonFinalExperimentAnalysis.model_validate(value)
    except ValidationError as exc:
        raise ValueError("Query experiment analysis contract is invalid") from exc
    raise ValueError("Query experiment analysis status is not canonical")


@register_agent
class ExperimentEvaluationAgent(BaseAgent):
    """Summarize verified snapshots without producing product decisions or effects."""

    name = "experiment_evaluation"
    description = "Summarize immutable, pipeline-complete experiment evidence."
    enabled = True
    order = 30
    requires = ()
    produces = "experiment_evidence_summaries"
    parse_as = "list"
    system_prompt = EXPERIMENT_EVALUATION_SYSTEM
    model_tier = "reasoning"

    async def gather(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
    ) -> dict[str, Any]:
        experiments = await get_active_experiments(ctx.project_id)
        completed = _completed_experiment_keys(experiments)
        selected = completed[:_MAX_EXPERIMENTS_PER_RUN]
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_QUERIES)

        async def fetch(experiment_key: str) -> tuple[str, Any]:
            async with semaphore:
                return experiment_key, await get_experiment_results(
                    experiment_key,
                    ctx.project_id,
                )

        raw_results = await asyncio.gather(*(fetch(key) for key in selected))
        verified_snapshots: list[dict[str, Any]] = []
        non_final_reasons: Counter[str] = Counter()
        for requested_key, raw in raw_results:
            analysis = _analysis_result(raw)
            if analysis.experiment_key != requested_key:
                raise ValueError("Query returned analysis for a different experiment")
            if isinstance(analysis, NonFinalExperimentAnalysis):
                non_final_reasons[analysis.reason] += 1
                continue
            digest = _snapshot_sha256(analysis)
            verified_snapshots.append(
                {
                    "experiment_id": analysis.experiment_key,
                    "source_snapshot_sha256": digest,
                    "snapshot": analysis.model_dump(mode="json"),
                }
            )

        return {
            "verified_snapshots": verified_snapshots,
            "completed_experiments": len(completed),
            "omitted_by_run_bound": max(0, len(completed) - len(selected)),
            "non_final_reasons": dict(sorted(non_final_reasons.items())),
        }

    def build_prompt(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
    ) -> str | None:
        snapshots = working.get("verified_snapshots")
        if not snapshots:
            return None
        return EXPERIMENT_EVALUATION_PROMPT.format(
            experiments=json.dumps(
                snapshots,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    def parse(self, response: str) -> list[dict[str, Any]]:
        raw = super().parse(response)
        try:
            return [
                ExperimentEvidenceNarrative.model_validate(item).model_dump(mode="json")
                for item in raw
            ]
        except ValidationError as exc:
            raise ValueError("experiment evidence summary output is invalid") from exc

    async def act(
        self,
        ctx: AgentContext,
        state: dict[str, Any],
        working: dict[str, Any],
        output: Any,
    ) -> dict[str, Any]:
        if not isinstance(output, list):
            raise ValueError("experiment evidence summaries must be an array")
        sources = working.get("verified_snapshots") or []
        source_by_id = {item["experiment_id"]: item for item in sources}
        if len(source_by_id) != len(sources):
            raise ValueError("verified experiment snapshot identities are not unique")
        narrative_by_id = {item["experiment_id"]: item for item in output}
        if len(narrative_by_id) != len(output):
            raise ValueError(
                "experiment evidence summaries contain duplicate identities"
            )
        if set(narrative_by_id) != set(source_by_id):
            raise ValueError(
                "experiment evidence summaries must cover the exact source set"
            )

        canonical: list[dict[str, Any]] = []
        for experiment_id in sorted(source_by_id):
            source = source_by_id[experiment_id]
            narrative = ExperimentEvidenceNarrative.model_validate(
                narrative_by_id[experiment_id]
            )
            if narrative.source_snapshot_sha256 != source["source_snapshot_sha256"]:
                raise ValueError(
                    "experiment evidence summary changed its snapshot identity"
                )
            canonical.append(
                ExperimentEvidenceSummary(
                    **narrative.model_dump(mode="python"),
                    source_snapshot=VerifiedExperimentSnapshot.model_validate(
                        source["snapshot"]
                    ),
                ).model_dump(mode="json")
            )
        output[:] = canonical
        return {
            "evidence_only": True,
            "summarized": len(canonical),
            "completed_experiments": working.get("completed_experiments", 0),
            "non_final_reasons": working.get("non_final_reasons", {}),
            "omitted_by_run_bound": working.get("omitted_by_run_bound", 0),
            "deployment_readiness_assessed": False,
            "mutations_attempted": 0,
            "needs_approval": False,
        }
