"""Evidence-only experiment evaluation over immutable Query snapshots."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.framework.registry import registered_agents
from app.graphs import experiment_evaluation as evaluation
from app.graphs.experiment_evaluation import ExperimentEvaluationAgent


def _config_experiment(key: str, status: str = "completed") -> dict:
    return {
        "key": key,
        "flag_key": key,
        "bucket_by": "anonymous_id",
        "status": status,
        "description": "Checkout evidence",
        "default_variant": "control",
        "traffic_percentage": 100.0,
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "targeting_rules": [],
        "primary_metric": {
            "event": "purchase",
            "type": "conversion",
            "direction": "increase",
        },
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.25,
            "minimum_detectable_effect": 0.1,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 20,
            "data_settlement_seconds": 300,
        },
        "start_date": "2026-06-01T00:00:00Z",
        "end_date": "2026-06-08T00:00:00Z",
        "version": 3,
        "created_at": "2026-05-31T00:00:00Z",
        "updated_at": "2026-06-08T00:05:00Z",
        "archived_at": None,
        "archived_by": None,
    }


def _snapshot(key: str = "exp_checkout") -> dict:
    return {
        "experiment_key": key,
        "flag_key": key,
        "experiment_status": "completed",
        "control_variant": "control",
        "metric_event": "purchase",
        "metric_direction": "increase",
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.25,
            "minimum_detectable_effect": 0.1,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 20,
            "data_settlement_seconds": 300,
        },
        "start_date": "2026-06-01T00:00:00Z",
        "end_date": "2026-06-08T00:00:00Z",
        "config_version": 3,
        "arms": [
            {
                "variant": "control",
                "sample_size": 20,
                "conversions": 5,
                "conversion_rate": 0.25,
            },
            {
                "variant": "treatment",
                "sample_size": 20,
                "conversions": 10,
                "conversion_rate": 0.5,
            },
        ],
        "crossover_actors": 0,
        "unknown_variant_actors": 0,
        "identity_conflict_actors": 0,
        "identity_quality": "unambiguous",
        "deployment_readiness": "not_assessed",
        "analysis_status": "decision_snapshot",
        "data_completeness": "verified",
        "inference_method": "fisher_exact_two_sided",
        "interval_method": "newcombe_wilson",
        "correction": "bonferroni",
        "comparisons": [
            {
                "control_variant": "control",
                "treatment_variant": "treatment",
                "control_rate": 0.25,
                "treatment_rate": 0.5,
                "rate_difference": 0.25,
                "confidence_interval": [-0.05, 0.55],
                "raw_p_value": 0.19,
                "adjusted_p_value": 0.19,
                "is_statistically_significant": False,
            }
        ],
    }


def _non_final(key: str = "exp_pending") -> dict:
    value = _snapshot(key)
    value.pop("comparisons")
    value.pop("inference_method")
    value.pop("interval_method")
    value.pop("correction")
    value.update(
        analysis_status="non_final",
        data_completeness="not_verified",
        reason="awaiting_pipeline_boundary",
        underpowered_variants=[],
    )
    return value


def test_experiment_evaluation_is_enabled_but_has_no_verdict_surface():
    registered = registered_agents()

    assert registered["experiment_evaluation"] is ExperimentEvaluationAgent
    assert ExperimentEvaluationAgent.enabled is True
    assert ExperimentEvaluationAgent.produces == "experiment_evidence_summaries"


@pytest.mark.parametrize("bucket_by", ["account_id", "", None])
def test_config_experiment_identity_is_exact_and_required(bucket_by) -> None:
    experiment = _config_experiment("exp_checkout")
    experiment["bucket_by"] = bucket_by

    with pytest.raises(ValueError, match="bucket_by is not canonical"):
        evaluation._completed_experiment_keys([experiment])

    del experiment["bucket_by"]
    with pytest.raises(ValueError, match="canonical list schema"):
        evaluation._completed_experiment_keys([experiment])


@pytest.mark.asyncio
async def test_gather_accepts_only_verified_completed_snapshots(monkeypatch):
    async def experiments(project_id: str):
        assert project_id == "demo"
        return [
            _config_experiment("exp_checkout"),
            _config_experiment("exp_pending"),
            _config_experiment("exp_running", "running"),
        ]

    async def result(experiment_id: str, project_id: str):
        assert project_id == "demo"
        return (
            _snapshot(experiment_id)
            if experiment_id == "exp_checkout"
            else _non_final()
        )

    monkeypatch.setattr(evaluation, "get_active_experiments", experiments)
    monkeypatch.setattr(evaluation, "get_experiment_results", result)

    working = await ExperimentEvaluationAgent().gather(
        SimpleNamespace(project_id="demo"),
        {},
        {},
    )

    assert working["completed_experiments"] == 2
    assert working["omitted_by_run_bound"] == 0
    assert working["non_final_reasons"] == {"awaiting_pipeline_boundary": 1}
    assert len(working["verified_snapshots"]) == 1
    source = working["verified_snapshots"][0]
    assert source["experiment_id"] == "exp_checkout"
    assert len(source["source_snapshot_sha256"]) == 64
    assert source["snapshot"]["data_completeness"] == "verified"


@pytest.mark.asyncio
async def test_gather_rejects_noncanonical_config_or_query_contract(monkeypatch):
    async def bad_experiments(_project_id: str):
        return [{**_config_experiment("exp_checkout"), "default_value": "control"}]

    monkeypatch.setattr(evaluation, "get_active_experiments", bad_experiments)
    with pytest.raises(ValueError, match="canonical list schema"):
        await ExperimentEvaluationAgent().gather(
            SimpleNamespace(project_id="demo"),
            {},
            {},
        )

    async def experiments(_project_id: str):
        return [_config_experiment("exp_checkout")]

    async def incomplete(_experiment_id: str, _project_id: str):
        return {**_snapshot(), "data_completeness": "not_verified"}

    monkeypatch.setattr(evaluation, "get_active_experiments", experiments)
    monkeypatch.setattr(evaluation, "get_experiment_results", incomplete)
    with pytest.raises(ValueError, match="analysis contract is invalid"):
        await ExperimentEvaluationAgent().gather(
            SimpleNamespace(project_id="demo"),
            {},
            {},
        )


def test_prompt_contains_only_verified_sources_and_skips_empty_work():
    agent = ExperimentEvaluationAgent()
    assert agent.build_prompt(object(), {}, {"verified_snapshots": []}) is None

    snapshot = evaluation.VerifiedExperimentSnapshot.model_validate(_snapshot())
    digest = evaluation._snapshot_sha256(snapshot)
    prompt = agent.build_prompt(
        object(),
        {},
        {
            "verified_snapshots": [
                {
                    "experiment_id": "exp_checkout",
                    "source_snapshot_sha256": digest,
                    "snapshot": snapshot.model_dump(mode="json"),
                }
            ]
        },
    )
    assert prompt is not None
    assert digest in prompt
    assert '"data_completeness":"verified"' in prompt


@pytest.mark.parametrize(
    "payload",
    [
        {
            "experiment_id": "exp_checkout",
            "source_snapshot_sha256": "a" * 64,
            "evidence_summary": "The treatment rate was higher in this fixed sample.",
            "limitations": ["Operational guardrails were not supplied."],
            "deployment_readiness": "not_assessed",
            "verdict": "ship",
        },
        {
            "experiment_id": "exp_checkout",
            "source_snapshot_sha256": "a" * 64,
            "evidence_summary": "The team should deploy the treatment.",
            "limitations": ["Operational guardrails were not supplied."],
            "deployment_readiness": "not_assessed",
        },
    ],
)
def test_parse_rejects_mutation_fields_and_product_decision_language(payload):
    with pytest.raises(ValueError, match="summary output is invalid"):
        ExperimentEvaluationAgent().parse(json.dumps([payload]))


@pytest.mark.asyncio
async def test_act_binds_summary_to_exact_snapshot_and_attempts_no_mutation():
    agent = ExperimentEvaluationAgent()
    snapshot = evaluation.VerifiedExperimentSnapshot.model_validate(_snapshot())
    digest = evaluation._snapshot_sha256(snapshot)
    working = {
        "verified_snapshots": [
            {
                "experiment_id": "exp_checkout",
                "source_snapshot_sha256": digest,
                "snapshot": snapshot.model_dump(mode="json"),
            }
        ],
        "completed_experiments": 1,
        "non_final_reasons": {},
        "omitted_by_run_bound": 0,
    }
    output = [
        {
            "experiment_id": "exp_checkout",
            "source_snapshot_sha256": digest,
            "evidence_summary": (
                "The observed rates differ by 0.25 and the adjusted p-value is 0.19."
            ),
            "limitations": [
                "The supplied evidence contains no operational guardrail assessment."
            ],
            "deployment_readiness": "not_assessed",
        }
    ]

    metadata = await agent.act(object(), {}, working, output)

    assert metadata["mutations_attempted"] == 0
    assert metadata["needs_approval"] is False
    assert metadata["deployment_readiness_assessed"] is False
    assert output[0]["schema_version"] == "experiment_evidence_summary@1"
    assert output[0]["source_snapshot"] == snapshot.model_dump(mode="json")


@pytest.mark.asyncio
async def test_act_rejects_omitted_or_rebound_snapshot_identity():
    snapshot = evaluation.VerifiedExperimentSnapshot.model_validate(_snapshot())
    digest = evaluation._snapshot_sha256(snapshot)
    working = {
        "verified_snapshots": [
            {
                "experiment_id": "exp_checkout",
                "source_snapshot_sha256": digest,
                "snapshot": snapshot.model_dump(mode="json"),
            }
        ]
    }
    agent = ExperimentEvaluationAgent()

    with pytest.raises(ValueError, match="exact source set"):
        await agent.act(object(), {}, working, [])

    rebound = [
        {
            "experiment_id": "exp_checkout",
            "source_snapshot_sha256": "b" * 64,
            "evidence_summary": "The fixed sample contains a rate difference.",
            "limitations": ["No operational guardrail assessment was supplied."],
            "deployment_readiness": "not_assessed",
        }
    ]
    with pytest.raises(ValueError, match="changed its snapshot identity"):
        await agent.act(object(), {}, working, rebound)
