"""Tests for the strict Config experiment-analysis client."""

import httpx
import pytest

from app import config_client
from app.config_client import (
    ConfigServiceUnavailable,
    ExperimentNotAnalyzable,
    ExperimentNotFound,
    fetch_experiment_analysis,
)

PROJECT_ID = "apiasport"
SERVICE_KEY = "proj_apiasport_0123456789abcdef"


def _metadata(key: str = "exp_123") -> dict:
    return {
        "key": key,
        "flag_key": "checkout-experiment",
        "status": "running",
        "control_variant": "control",
        "variants": ["control", "treatment"],
        "metric_event": "purchase",
        "metric_direction": "increase",
        "statistical_plan": {
            "protocol": "fixed_horizon_fisher_newcombe_cc_plan_v1",
            "baseline_conversion_rate": 0.5,
            "minimum_detectable_effect": 0.5,
            "significance_level": 0.05,
            "nominal_power": 0.8,
            "required_sample_size_per_arm": 20,
            "data_settlement_seconds": 5,
        },
        "start_date": "2025-01-01T00:00:00Z",
        "end_date": "2025-01-15T00:00:00Z",
        "version": 7,
    }


def _install_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(config_client.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(config_client, "CONFIG_SERVICE_URL", "https://config.test")


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_delegates_scoped_key(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v1/experiments/exp.key-1/analysis"
        assert not request.url.params
        assert request.headers["x-api-key"] == SERVICE_KEY
        return httpx.Response(200, json=_metadata("exp.key-1"))

    _install_transport(monkeypatch, handler)

    result = await fetch_experiment_analysis(PROJECT_ID, "exp.key-1", SERVICE_KEY)

    assert result.key == "exp.key-1"
    assert result.metric_event == "purchase"


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_maps_not_found(monkeypatch):
    _install_transport(
        monkeypatch,
        lambda _: httpx.Response(404, json={"message": "experiment missing"}),
    )

    with pytest.raises(ExperimentNotFound, match="experiment missing"):
        await fetch_experiment_analysis(PROJECT_ID, "missing", SERVICE_KEY)


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_maps_conflict(monkeypatch):
    _install_transport(
        monkeypatch,
        lambda _: httpx.Response(409, json={"message": "draft is not analyzable"}),
    )

    with pytest.raises(ExperimentNotAnalyzable, match="draft is not analyzable"):
        await fetch_experiment_analysis(PROJECT_ID, "draft", SERVICE_KEY)


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_fails_closed_on_network_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    _install_transport(monkeypatch, handler)

    with pytest.raises(ConfigServiceUnavailable, match="request failed"):
        await fetch_experiment_analysis(PROJECT_ID, "exp_123", SERVICE_KEY)


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_rejects_extra_fields(monkeypatch):
    payload = _metadata()
    payload["recommendation"] = "ship"
    _install_transport(
        monkeypatch,
        lambda _: httpx.Response(200, json=payload),
    )

    with pytest.raises(ConfigServiceUnavailable, match="invalid.*contract"):
        await fetch_experiment_analysis(PROJECT_ID, "exp_123", SERVICE_KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("significance_level", "0.05"),
        ("nominal_power", "0.8"),
        ("required_sample_size_per_arm", "20"),
        ("data_settlement_seconds", "5"),
    ],
)
async def test_fetch_experiment_analysis_rejects_coerced_plan_numbers(
    monkeypatch,
    field,
    value,
):
    payload = _metadata()
    payload["statistical_plan"][field] = value
    _install_transport(monkeypatch, lambda _: httpx.Response(200, json=payload))

    with pytest.raises(ConfigServiceUnavailable, match="invalid.*contract"):
        await fetch_experiment_analysis(PROJECT_ID, "exp_123", SERVICE_KEY)


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_rejects_mismatched_key(monkeypatch):
    _install_transport(
        monkeypatch,
        lambda _: httpx.Response(200, json=_metadata("another-experiment")),
    )

    with pytest.raises(ConfigServiceUnavailable, match="different experiment"):
        await fetch_experiment_analysis(PROJECT_ID, "exp_123", SERVICE_KEY)


@pytest.mark.asyncio
async def test_fetch_experiment_analysis_rejects_cross_project_delegation(monkeypatch):
    _install_transport(
        monkeypatch,
        lambda _: pytest.fail("Config must not receive a cross-project key"),
    )

    with pytest.raises(ConfigServiceUnavailable, match="credential is unavailable"):
        await fetch_experiment_analysis(
            PROJECT_ID,
            "exp_123",
            "proj_another_0123456789abcdef",
        )
