"""End-to-end client behavior with a fake transport."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import RecordingTransport, make_flag

from apdl import APDL, APDLClient, APDLConfig
from apdl.config import MAX_BATCH_SIZE
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
    base = dict(
        api_key="proj_test_0123456789abcdef",
        endpoint="https://apdl.test",
        enable_flags=False,
    )
    base.update(cfg)
    return APDLClient(APDLConfig(**base), transport=transport)


def _ingestion_validator():
    """Import the real monorepo Ingestion validator for wire-contract tests."""
    root = Path(__file__).resolve().parents[3]
    ingestion = str(root / "services" / "ingestion")
    if ingestion not in sys.path:
        sys.path.insert(0, ingestion)
    from app.validation.schema import validate_single_event

    return validate_single_event


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


def test_identify_with_both_ids_emits_the_canonical_alias_assertion():
    transport = RecordingTransport()
    client = make_client(transport)

    client.identify("u1", {"plan": "pro"}, anonymous_id="anon1")
    client.flush()
    client.shutdown()

    [event] = transport.all_events()
    assert event["type"] == "identify"
    assert event["user_id"] == "u1"
    assert event["anonymous_id"] == "anon1"
    assert event["traits"] == {"plan": "pro"}
    assert _ingestion_validator()(event)["valid"] is True


def test_event_requires_identity():
    client = make_client(RecordingTransport())
    with pytest.raises(ValueError):
        client.track("x")
    client.shutdown()


def test_non_json_event_is_rejected_synchronously_before_queueing():
    transport = RecordingTransport()
    client = make_client(transport)
    cycle: dict = {}
    cycle["self"] = cycle

    with pytest.raises(ValueError, match="cyclic JSON"):
        client.track("x", cycle, user_id="u1")

    assert client.pending_events == 0
    assert transport.posts == []
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


def test_exposure_queue_failure_does_not_change_variant_or_commit_dedupe():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True, max_queue_size=1)
    client.set_flags([make_flag("g")])
    client.track("queue_filler", user_id="u1")

    expected = client.get_variant("g", user_id="u1", log_exposure=False)
    assert client.get_variant("g", user_id="u1") == expected
    assert client.pending_events == 1

    client.flush()
    assert client.get_variant("g", user_id="u1") == expected
    client.flush()
    client.shutdown()

    exposures = [
        event
        for event in transport.all_events()
        if event["event"] == FEATURE_FLAG_EXPOSURE_EVENT
    ]
    assert len(exposures) == 1


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
        "project_id": "test",
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


def test_refresh_flags_rejects_foreign_project_envelope():
    payload = {
        "schema_version": 2,
        "project_id": "other",
        "flags": [
            {
                "key": "foreign",
                "enabled": True,
                "default_variant": "control",
                "variants": [
                    {"key": "control", "weight": 1},
                    {"key": "treatment", "weight": 1},
                ],
                "salt": "s",
                "rules": [],
                "fallthrough": {
                    "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
                },
                "version": 1,
            }
        ],
    }
    client = make_client(RecordingTransport(flags=payload))

    assert client.refresh_flags() is False
    assert client.get_variant("foreign", user_id="u1", log_exposure=False) is None
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


# ── API key validation ────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "   ",
        "secret_only",
        "proj_demo_short",  # secret < 16 chars
        "proj__0123456789abcdef",  # empty project id
        "proj_demo_with-dashes-0123456789",  # non-alphanumeric secret
        "client_demo_0123456789abcdef",  # browser-only credential family
    ],
)
def test_rejects_malformed_api_key(bad_key):
    with pytest.raises(ValueError):
        APDLConfig(api_key=bad_key, endpoint="https://apdl.test")


def test_requires_an_explicit_endpoint():
    with pytest.raises(ValueError, match="endpoint"):
        APDLConfig(api_key="proj_test_0123456789abcdef")

    with pytest.raises(ValueError, match="api_key and endpoint are required"):
        APDL.init(api_key="proj_test_0123456789abcdef")


def test_init_accepts_explicit_key_and_endpoint():
    client = APDL.init(
        api_key="proj_test_0123456789abcdef",
        endpoint="https://apdl.test",
        enable_flags=False,
        transport=RecordingTransport(),
    )

    assert client.project_id == "test"
    client.shutdown()


def test_client_rejects_competing_config_and_explicit_options():
    config = APDLConfig(
        api_key="proj_test_0123456789abcdef",
        endpoint="https://apdl.test",
        enable_flags=False,
    )

    with pytest.raises(ValueError, match="either config or explicit"):
        APDLClient(config, endpoint="https://other.test")


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "api.apdl.test",
        "ftp://apdl.test",
        "https://user:secret@apdl.test",
        "https://:443",
        "https://apdl.test:not-a-port",
        "https://apdl.test/v1",
        "https://apdl.test?tenant=one",
        "https://apdl.test#fragment",
        " https://apdl.test",
    ],
)
def test_rejects_noncanonical_endpoint(endpoint):
    with pytest.raises(ValueError, match="endpoint"):
        APDLConfig(api_key="proj_test_0123456789abcdef", endpoint=endpoint)


def test_endpoint_accepts_an_http_origin_and_removes_a_trailing_slash():
    config = APDLConfig(
        api_key="proj_test_0123456789abcdef",
        endpoint="http://localhost:8000/",
    )

    assert config.endpoint == "http://localhost:8000"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", b"proj_test_0123456789abcdef"),
        ("endpoint", b"https://apdl.test"),
        ("batch_size", "20"),
        ("flush_interval", "3.0"),
        ("max_queue_size", "1000"),
        ("enable_flags", "false"),
        ("flag_poll_interval", "30.0"),
        ("log_exposures", "true"),
        ("request_timeout", "10.0"),
        ("debug", "off"),
    ],
)
def test_config_rejects_coercive_scalar_inputs(field, value):
    values = {
        "api_key": "proj_test_0123456789abcdef",
        "endpoint": "https://apdl.test",
        field: value,
    }

    with pytest.raises(ValueError):
        APDLConfig(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("batch_size", 0),
        ("batch_size", MAX_BATCH_SIZE + 1),
        ("max_queue_size", 0),
        ("flush_interval", 0.0),
        ("flag_poll_interval", -1.0),
        ("request_timeout", 0.0),
    ],
)
def test_config_rejects_values_outside_explicit_bounds(field, value):
    with pytest.raises(ValueError):
        APDLConfig(
            api_key="proj_test_0123456789abcdef",
            endpoint="https://apdl.test",
            **{field: value},
        )


def test_exposes_project_id_from_api_key():
    transport = RecordingTransport()
    client = APDLClient(
        APDLConfig(
            api_key="proj_acme42_0123456789abcdef",
            endpoint="https://apdl.test",
            enable_flags=False,
        ),
        transport=transport,
    )
    assert client.project_id == "acme42"
    client.shutdown()


# ── Bulk evaluation ───────────────────────────────────────────


def test_get_all_variants_evaluates_every_cached_flag():
    client = make_client(RecordingTransport())
    client.set_flags([make_flag("a"), make_flag("b")])
    variants = client.get_all_variants(user_id="u1")
    assert set(variants) == {"a", "b"}
    assert all(v in {"control", "treatment"} for v in variants.values())
    client.shutdown()


def test_get_all_variants_never_logs_exposures():
    transport = RecordingTransport()
    client = make_client(transport, log_exposures=True)
    client.set_flags([make_flag("a"), make_flag("b")])
    client.get_all_variants(user_id="u1")
    client.flush()
    client.shutdown()
    assert transport.all_events() == []


def test_get_all_variant_details_is_sorted_and_explained():
    client = make_client(RecordingTransport())
    client.set_flags([make_flag("zeta"), make_flag("alpha")])
    details = client.get_all_variant_details(user_id="u1")
    assert [d.key for d in details] == ["alpha", "zeta"]
    assert all(d.reason == "fallthrough" for d in details)
    client.shutdown()
