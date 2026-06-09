"""End-to-end client behavior with a fake transport."""

from __future__ import annotations

import pytest
from conftest import RecordingTransport, make_gate

from apdl import APDLClient, APDLConfig
from apdl.types import FEATURE_FLAG_EXPOSURE_EVENT


def make_client(transport: RecordingTransport, **cfg) -> APDLClient:
    base = dict(api_key="proj_test_secret", enable_flags=False)
    base.update(cfg)
    return APDLClient(APDLConfig(**base), transport=transport)


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
    # None optionals are omitted from the wire payload.
    assert "group_id" not in ev


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


def test_check_gate_uses_local_cache():
    client = make_client(RecordingTransport())
    client.set_flags([make_gate("new-checkout", default_value=False)])  # fallthrough True
    assert client.check_gate("new-checkout", user_id="u1", log_exposure=False) is True
    assert client.check_gate("missing", user_id="u1", log_exposure=False) is False
    client.shutdown()


def test_exposure_logged_once_per_identity_version_value():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.set_flags([make_gate("g")])
    client.check_gate("g", user_id="u1")
    client.check_gate("g", user_id="u1")  # deduped
    client.check_gate("g", user_id="u2")  # distinct identity -> logged
    client.flush()
    client.shutdown()

    exposures = [e for e in transport.all_events() if e["event"] == FEATURE_FLAG_EXPOSURE_EVENT]
    assert len(exposures) == 2
    assert exposures[0]["properties"]["flag_key"] == "g"


def test_not_found_gate_does_not_log_exposure():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.check_gate("ghost", user_id="u1")
    client.flush()
    client.shutdown()
    assert transport.all_events() == []


def test_refresh_flags_from_transport():
    gate_payload = {
        "flags": [{
            "key": "g",
            "enabled": True,
            "default_value": False,
            "salt": "s",
            "rules": [],
            "fallthrough": {"value": True, "rollout": {"percentage": 100.0, "bucket_by": "user_id"}},
            "version": 1,
        }]
    }
    transport = RecordingTransport(flags=gate_payload)
    client = make_client(transport)
    assert client.refresh_flags() is True
    assert client.check_gate("g", user_id="u1", log_exposure=False) is True
    client.shutdown()


def test_on_flag_change_fires_on_update():
    transport = RecordingTransport()
    client = make_client(transport)
    calls = []
    unsubscribe = client.on_flag_change("g", lambda: calls.append(True))
    client.set_flags([make_gate("g")])
    assert calls == [True]
    unsubscribe()
    client.set_flags([make_gate("g", version=2)])
    assert calls == [True]  # no further calls after unsubscribe
    client.shutdown()


def test_context_manager_shuts_down():
    transport = RecordingTransport()
    with make_client(transport) as client:
        client.track("e", user_id="u1")
    assert transport.closed is True
