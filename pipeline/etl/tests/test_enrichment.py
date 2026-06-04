"""Enricher chain: built-ins, ordering/merge, and best-effort failure."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from etl import (
    BaseEnricher,
    EtlContext,
    get_enricher,
    register_enricher,
    run_enrichers,
)
from tests.conftest import RECEIVED_AT


def _env(payload):
    return SimpleNamespace(payload=payload)


def test_device_enricher_desktop_chrome():
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT, extra={
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537"
    })
    out = get_enricher("device").enrich(_env({}), ctx)
    assert out == {"device_type": "desktop", "browser": "Chrome", "os_name": "Windows"}


def test_device_enricher_ios_is_mobile_not_macos():
    # iOS UAs contain "like Mac OS X"; must resolve to iOS / mobile.
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT, extra={
        "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) CriOS/120"
    })
    out = get_enricher("device").enrich(_env({}), ctx)
    assert out["device_type"] == "mobile"
    assert out["os_name"] == "iOS"
    assert out["browser"] == "Chrome"  # CriOS == Chrome on iOS


def test_device_enricher_ipad_is_tablet():
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT, extra={
        "user_agent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) Safari/604"
    })
    assert get_enricher("device").enrich(_env({}), ctx)["device_type"] == "tablet"


def test_device_enricher_no_ua_returns_empty():
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT)
    assert get_enricher("device").enrich(_env({}), ctx) == {}


def test_geo_enricher_normalises_country():
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT)
    env = _env({"context": {"geo": {"country": "us", "region": "CA"}}})
    assert get_enricher("geo").enrich(env, ctx) == {"country": "US", "region": "CA"}


def test_geo_enricher_empty_when_no_signal():
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT)
    assert get_enricher("geo").enrich(_env({}), ctx) == {}


def test_run_enrichers_merges_later_wins():
    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT, extra={
        "user_agent": "Mozilla/5.0 (Windows NT 10.0) Firefox/130"
    })
    env = _env({"context": {"geo": {"country": "gb"}}})
    out = run_enrichers(("device", "geo"), env, ctx)
    assert out["browser"] == "Firefox"
    assert out["country"] == "GB"


def test_run_enrichers_skips_failing_enricher():
    @register_enricher
    class Boom(BaseEnricher):
        name = "boom_test"

        def enrich(self, envelope, ctx):
            raise RuntimeError("kaboom")

    ctx = EtlContext(project_id=1, received_at=RECEIVED_AT)
    # A failing enricher is logged and skipped; the chain still returns.
    assert run_enrichers(("boom_test", "geo"), _env({}), ctx) == {}


def test_unknown_enricher_raises():
    with pytest.raises(KeyError):
        get_enricher("nope")
