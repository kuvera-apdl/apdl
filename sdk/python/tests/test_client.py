"""End-to-end client behavior with a fake transport."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import RecordingTransport, make_flag

from apdl import APDLClient, APDLConfig
from apdl.types import FEATURE_FLAG_EXPOSURE_EVENT

CANONICAL_EXPOSURE_KEYS = {
    "flag_key",
    "variant",
    "reason",
    "rule_id",
    "rollout_bucket",
    "variant_bucket",
    "rollout_percentage",
    "bucket_by",
    "config_version",
    "source",
    "page",
    "component",
}


def make_client(transport: RecordingTransport, **cfg) -> APDLClient:
    base = dict(api_key="proj_test_secret", enable_flags=False)
    base.update(cfg)
    return APDLClient(APDLConfig(**base), transport=transport)


def _ingestion_validator():
    """The real ingestion validator, or None if the service is not importable."""
    root = Path(__file__).resolve().parents[3]
    ingestion = str(root / "services" / "ingestion")
    if ingestion not in sys.path:
        sys.path.insert(0, ingestion)
    try:
        from app.validation.schema import validate_single_event

        return validate_single_event
    except Exception:  # pragma: no cover - only when run outside the monorepo
        return None


# ── Event tracking ────────────────────────────────────────────


def test_track_emits_canonical_payload():
    transport = RecordingTransport()
    client = make_client(transport)
    client.track("order_completed", {"total": 42}, user_id="u1")
    client.flush()
    client.shutdown()

    events = transport.all_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "order_completed"
    assert ev["type"] == "track"
    assert ev["user_id"] == "u1"
    assert ev["properties"] == {"total": 42}
    assert ev["context"]["library"]["name"] == "apdl-python"
    assert "message_id" in ev and "timestamp" in ev
    # None optionals (incl. session_id for non-reserved events) are omitted.
    assert "group_id" not in ev
    assert "session_id" not in ev


def test_identify_and_group_and_page():
    transport = RecordingTransport()
    client = make_client(transport)
    client.identify("u1", {"plan": "pro"})
    client.group("org1", {"name": "Acme"}, user_id="u1")
    client.page("/home", {"ref": "google"}, user_id="u1")
    client.flush()
    client.shutdown()

    by_type = {e["type"]: e for e in transport.all_events()}
    assert by_type["identify"]["traits"] == {"plan": "pro"}
    assert by_type["group"]["group_id"] == "org1"
    assert by_type["page"]["properties"]["name"] == "/home"


def test_event_requires_identity():
    client = make_client(RecordingTransport())
    with pytest.raises(ValueError):
        client.track("x")
    client.shutdown()


# ── Feature flags ─────────────────────────────────────────────


def test_get_variant_uses_local_cache():
    client = make_client(RecordingTransport())
    client.set_flags([make_flag("new-checkout")])
    assert client.get_variant("new-checkout", user_id="u1", log_exposure=False) in {
        "control",
        "treatment",
    }
    assert client.get_variant("missing", user_id="u1", log_exposure=False) is None
    client.shutdown()


def test_get_variant_is_deterministic():
    client = make_client(RecordingTransport())
    client.set_flags([make_flag("g")])
    first = client.get_variant("g", user_id="u1", log_exposure=False)
    second = client.get_variant("g", user_id="u1", log_exposure=False)
    assert first == second
    client.shutdown()


def test_exposure_logged_once_per_identity_version_variant():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.set_flags([make_flag("g")])
    client.get_variant("g", user_id="u1")
    client.get_variant("g", user_id="u1")  # deduped
    client.get_variant("g", user_id="u2")  # distinct identity -> logged
    client.flush()
    client.shutdown()

    exposures = [
        e for e in transport.all_events() if e["event"] == FEATURE_FLAG_EXPOSURE_EVENT
    ]
    assert len(exposures) == 2
    props = exposures[0]["properties"]
    assert set(props) == CANONICAL_EXPOSURE_KEYS
    assert props["flag_key"] == "g"
    assert "value" not in props and "bucket" not in props
    assert isinstance(exposures[0]["session_id"], str) and exposures[0]["session_id"]


def test_exposure_carries_page_and_component():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.set_flags([make_flag("g")])
    client.get_variant("g", user_id="u1", page="/checkout", component="CheckoutPage")
    client.flush()
    client.shutdown()

    exposure = next(
        e for e in transport.all_events() if e["event"] == FEATURE_FLAG_EXPOSURE_EVENT
    )
    assert exposure["properties"]["page"] == "/checkout"
    assert exposure["properties"]["component"] == "CheckoutPage"


def test_exposure_passes_real_ingestion_validator():
    validate = _ingestion_validator()
    if validate is None:
        pytest.skip("ingestion service not importable in isolation")

    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.set_flags([make_flag("g")])
    client.get_variant("g", user_id="u1", page="/p", component="C")
    client.flush()
    client.shutdown()

    exposure = next(
        e for e in transport.all_events() if e["event"] == FEATURE_FLAG_EXPOSURE_EVENT
    )
    result = validate(exposure)
    assert result["valid"] is True, result["errors"]


def test_not_found_flag_does_not_log_exposure():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.get_variant("ghost", user_id="u1")
    client.flush()
    client.shutdown()
    assert transport.all_events() == []


def test_exposure_skipped_without_identity():
    # bucket_by a custom attribute so a variant is assigned without user identity,
    # but an exposure can't be attributed -> not logged.
    from apdl.flags.models import FallthroughConfig, RolloutConfig

    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.set_flags([
        make_flag(
            "g",
            fallthrough=FallthroughConfig(
                rollout=RolloutConfig(percentage=100.0, bucket_by="device_id")
            ),
        )
    ])
    result = client.get_variant_details("g", attributes={"device_id": "d1"})
    assert result.variant in {"control", "treatment"}
    client.flush()
    client.shutdown()
    assert transport.all_events() == []


def test_refresh_flags_from_v2_envelope():
    payload = {
        "schema_version": 2,
        "project_id": "p1",
        "flags": [
            {
                "key": "g",
                "enabled": True,
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 1},
                    {"key": "treatment", "weight": 1},
                ],
                "salt": "s",
                "rules": [],
                "fallthrough": {"rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
                "version": 1,
            }
        ],
    }
    transport = RecordingTransport(flags=payload)
    client = make_client(transport)
    assert client.refresh_flags() is True
    assert client.get_variant("g", user_id="u1", log_exposure=False) in {"control", "treatment"}
    client.shutdown()


def test_refresh_flags_rejects_legacy_payload():
    legacy = {"flags": [{"key": "g", "enabled": True, "default_value": False}]}  # bare list-style
    transport = RecordingTransport(flags=legacy)
    client = make_client(transport)
    assert client.refresh_flags() is False
    assert client.get_variant("g", user_id="u1", log_exposure=False) is None
    client.shutdown()


def test_on_variant_change_fires_on_update():
    transport = RecordingTransport()
    client = make_client(transport)
    calls = []
    unsubscribe = client.on_variant_change("g", lambda: calls.append(True))
    client.set_flags([make_flag("g")])
    assert calls == [True]
    unsubscribe()
    client.set_flags([make_flag("g", version=2)])
    assert calls == [True]  # no further calls after unsubscribe
    client.shutdown()


def test_context_manager_shuts_down():
    transport = RecordingTransport()
    with make_client(transport) as client:
        client.track("e", user_id="u1")
    assert transport.closed is True
