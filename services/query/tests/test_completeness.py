import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.completeness import (
    _validate_snapshot_payload,
    boundary_token,
    get_or_create_experiment_boundary,
    parse_stream_id,
)


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, *, boundary, watermark=None, snapshot=None):
        self.boundary = boundary
        self.watermark = watermark
        self.snapshot = snapshot
        self.executions = []

    def transaction(self):
        return _Context(self)

    async def execute(self, query, *args):
        self.executions.append((query, args))
        return "INSERT 0 1"

    async def fetchrow(self, query, *_args):
        if "FROM experiment_analysis_snapshots" in query:
            return self.snapshot
        if "FROM event_pipeline_watermarks" in query:
            return self.watermark
        if "FROM experiment_analysis_boundaries" in query:
            return self.boundary
        raise AssertionError(query)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Context(self.connection)


def _boundary(marker_stream_id="10-2"):
    return {
        "stream_key": "events:raw:demo",
        "window_start": datetime(2025, 1, 1, tzinfo=UTC),
        "window_end": datetime(2025, 1, 2, tzinfo=UTC),
        "marker_token": boundary_token(
            project_id="demo",
            experiment_key="experiment",
            config_version=3,
            window_start=datetime(2025, 1, 1, tzinfo=UTC),
            window_end=datetime(2025, 1, 2, tzinfo=UTC),
        ),
        "marker_stream_id": marker_stream_id,
    }


def test_stream_ids_use_numeric_not_lexical_ordering():
    assert parse_stream_id("10-2") > parse_stream_id("9-999")
    with pytest.raises(ValueError):
        parse_stream_id("010-2")
    with pytest.raises(ValueError, match="unsigned range"):
        parse_stream_id(f"{2**64}-0")


def _snapshot_payload():
    return {
        "experiment_key": "experiment",
        "flag_key": "checkout",
        "experiment_status": "completed",
        "control_variant": "control",
        "metric_event": "purchase",
        "metric_direction": "increase",
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.1,
            "minimum_detectable_effect": 0.05,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 100,
            "data_settlement_seconds": 60,
        },
        "start_date": "2025-01-01T00:00:00Z",
        "end_date": "2025-01-02T00:00:00Z",
        "config_version": 3,
        "arms": [
            {
                "variant": "control",
                "sample_size": 100,
                "conversions": 10,
                "conversion_rate": 0.1,
            },
            {
                "variant": "treatment",
                "sample_size": 100,
                "conversions": 20,
                "conversion_rate": 0.2,
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
                "control_rate": 0.1,
                "treatment_rate": 0.2,
                "rate_difference": 0.1,
                "confidence_interval": [0.0, 0.2],
                "raw_p_value": 0.05,
                "adjusted_p_value": 0.05,
                "is_statistically_significant": True,
            }
        ],
    }


def test_frozen_snapshot_payload_must_match_its_digest():
    payload = _snapshot_payload()
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()

    assert _validate_snapshot_payload(payload, digest).data_completeness == "verified"
    with pytest.raises(RuntimeError, match="does not match"):
        _validate_snapshot_payload(payload, "0" * 64)


def test_boundary_token_is_stable_across_equivalent_timezones():
    utc_token = boundary_token(
        project_id="demo",
        experiment_key="experiment",
        config_version=3,
        window_start=datetime.fromisoformat("2025-01-01T00:00:00+00:00"),
        window_end=datetime.fromisoformat("2025-01-02T00:00:00+00:00"),
    )
    offset_token = boundary_token(
        project_id="demo",
        experiment_key="experiment",
        config_version=3,
        window_start=datetime.fromisoformat("2024-12-31T19:00:00-05:00"),
        window_end=datetime.fromisoformat("2025-01-01T19:00:00-05:00"),
    )
    assert utc_token == offset_token


@pytest.mark.asyncio
async def test_boundary_is_covered_only_by_healthy_contiguous_frontier():
    connection = _Connection(
        boundary=_boundary(),
        watermark={
            "stream_key": "events:raw:demo",
            "provenance_start_stream_id": "0-0",
            "contiguous_stream_id": "10-2",
            "status": "healthy",
            "failure_reason": None,
        },
    )

    authority = await get_or_create_experiment_boundary(
        _Pool(connection),
        project_id="demo",
        experiment_key="experiment",
        config_version=3,
        window_start=datetime(2025, 1, 1, tzinfo=UTC),
        window_end=datetime(2025, 1, 2, tzinfo=UTC),
    )

    assert authority.state == "covered"
    assert authority.marker_stream_id_parts == (10, 2)


@pytest.mark.asyncio
async def test_degraded_watermark_never_covers_boundary():
    connection = _Connection(
        boundary=_boundary(),
        watermark={
            "stream_key": "events:raw:demo",
            "provenance_start_stream_id": "0-0",
            "contiguous_stream_id": "10-2",
            "status": "degraded",
            "failure_reason": "dead_lettered_event",
        },
    )

    authority = await get_or_create_experiment_boundary(
        _Pool(connection),
        project_id="demo",
        experiment_key="experiment",
        config_version=3,
        window_start=datetime(2025, 1, 1, tzinfo=UTC),
        window_end=datetime(2025, 1, 2, tzinfo=UTC),
    )

    assert authority.state == "degraded"
    assert authority.failure_reason == "dead_lettered_event"
