"""Canonical public event-contract validation tests.

Tests validate_event_batch() and validate_single_event() directly,
covering all 24+ test cases from the C++ test suite.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.models.schemas import EventBatch
from app.validation.schema import validate_event_batch, validate_single_event

CONTRACT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "events" / "canonical.json"
)


def canonical_event(
    event: str = "test_event",
    event_type: str = "track",
    **overrides,
):
    value = {
        "event": event,
        "type": event_type,
        "anonymous_id": "anon_test",
        "timestamp": "2026-07-13T12:00:00.000Z",
        "context": {},
        "message_id": "message-test",
    }
    value.update(overrides)
    return value


# =====================================================================
# Batch validation tests
# =====================================================================


class TestBatchValidation:
    """Ported from SchemaValidationTest batch tests."""

    def test_valid_minimal_batch(self):
        """ValidMinimalBatch"""
        body = {"events": [canonical_event("click", user_id="u1")]}
        result = validate_event_batch(body)
        assert result["valid"] is True
        assert len(result["errors"]) == 0

    def test_batch_missing_events_key(self):
        """BatchMissingEventsKey"""
        body = {"items": []}
        result = validate_event_batch(body)
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["field"] == "events"
        assert "Missing" in result["errors"][0]["message"]

    def test_batch_events_not_array(self):
        """BatchEventsNotArray"""
        body = {"events": "not_array"}
        result = validate_event_batch(body)
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["field"] == "events"

    def test_batch_empty(self):
        """BatchEmpty"""
        body = {"events": []}
        result = validate_event_batch(body)
        assert result["valid"] is False

    def test_batch_exceeds_max_size(self):
        """BatchExceedsMaxSize -- 101 events should be rejected."""
        events = [canonical_event(message_id=f"message-{i}") for i in range(101)]
        body = {"events": events}
        result = validate_event_batch(body)
        assert result["valid"] is False
        found_size_error = any(
            "exceeds maximum" in e["message"] for e in result["errors"]
        )
        assert found_size_error

    def test_batch_at_max_size(self):
        """BatchAtMaxSize -- exactly 100 events should be valid."""
        events = [canonical_event(message_id=f"message-{i}") for i in range(100)]
        body = {"events": events}
        result = validate_event_batch(body)
        assert result["valid"] is True

    def test_batch_rejects_every_later_duplicate_message_id(self):
        body = {
            "events": [
                canonical_event(message_id="message-duplicate"),
                canonical_event(message_id="message-duplicate"),
                canonical_event(message_id="message-duplicate"),
            ],
        }

        result = validate_event_batch(body)

        assert result["valid"] is False
        duplicate_errors = [
            error
            for error in result["errors"]
            if error["message"].startswith("Duplicate message_id;")
        ]
        assert duplicate_errors == [
            {
                "field": "events[1].message_id",
                "message": (
                    "Duplicate message_id; first occurrence at "
                    "events[0].message_id"
                ),
            },
            {
                "field": "events[2].message_id",
                "message": (
                    "Duplicate message_id; first occurrence at "
                    "events[0].message_id"
                ),
            },
        ]

    def test_batch_accepts_distinct_message_ids(self):
        body = {
            "events": [
                canonical_event(message_id="message-one"),
                canonical_event(message_id="message-two"),
                canonical_event(message_id="message-three"),
            ],
        }

        result = validate_event_batch(body)

        assert result == {"valid": True, "errors": []}

    def test_duplicate_message_ids_do_not_hide_other_validation_errors(self):
        duplicate = canonical_event(message_id="message-duplicate")
        duplicate["unknown_field"] = True
        body = {
            "events": [
                canonical_event(message_id="message-duplicate"),
                duplicate,
            ],
        }

        result = validate_event_batch(body)

        assert result["valid"] is False
        assert {
            (error["field"], error["message"])
            for error in result["errors"]
        } >= {
            ("events[1].unknown_field", "Extra inputs are not permitted"),
            (
                "events[1].message_id",
                "Duplicate message_id; first occurrence at events[0].message_id",
            ),
        }

    def test_event_batch_model_rejects_every_later_duplicate_message_id(self):
        body = {
            "events": [
                canonical_event(message_id="message-duplicate"),
                canonical_event(message_id="message-duplicate"),
                canonical_event(message_id="message-duplicate"),
            ],
        }

        with pytest.raises(PydanticValidationError) as exc_info:
            EventBatch.model_validate(body, strict=True)

        duplicate_errors = exc_info.value.errors()
        assert [error["loc"] for error in duplicate_errors] == [
            ("events", 1, "message_id"),
            ("events", 2, "message_id"),
        ]
        assert [error["msg"] for error in duplicate_errors] == [
            (
                "Value error, Duplicate message_id; first occurrence at "
                "events[0].message_id"
            ),
            (
                "Value error, Duplicate message_id; first occurrence at "
                "events[0].message_id"
            ),
        ]

    def test_non_object_body(self):
        """NonObjectBody -- a string is not a dict."""
        result = validate_event_batch("just a string")
        assert result["valid"] is False
        assert len(result["errors"]) >= 1
        assert result["errors"][0]["field"] == "body"

    def test_shared_contract_fixture(self):
        fixture = json.loads(CONTRACT_FIXTURE.read_text())
        for event in fixture["valid"]:
            result = validate_single_event(event)
            assert result["valid"] is True, result["errors"]
        for case in fixture["invalid"]:
            result = validate_single_event(case["event"])
            assert result["valid"] is False, case["name"]

    def test_non_object_body_list(self):
        """NonObjectBody variant -- a list is not a dict."""
        result = validate_event_batch([1, 2, 3])
        assert result["valid"] is False
        assert len(result["errors"]) >= 1
        assert result["errors"][0]["field"] == "body"


# =====================================================================
# Single event validation tests
# =====================================================================


class TestSingleEventValidation:
    """Ported from SchemaValidationTest single-event tests."""

    def test_valid_track_event(self):
        """ValidTrackEvent"""
        event = canonical_event(
            "purchase",
            user_id="usr_42",
            properties={"amount": 99.99, "currency": "USD"},
            timestamp="2025-06-15T10:30:00.000Z",
        )
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_valid_feature_flag_exposure_event(self):
        event = feature_flag_exposure_event()
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_valid_server_feature_flag_exposure_event(self):
        event = feature_flag_exposure_event()
        event["properties"]["source"] = "server"

        result = validate_single_event(event)

        assert result["valid"] is True

    def test_valid_frontend_error_event(self):
        event = frontend_error_event()
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_frontend_error_rejects_unknown_properties(self):
        event = frontend_error_event()
        event["properties"]["activeFlags"] = {"checkout-gate": True}

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "properties.activeFlags" for error in result["errors"])

    def test_frontend_error_requires_matching_active_flag_versions(self):
        event = frontend_error_event()
        event["properties"]["active_flag_versions"] = {}

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(
            error["field"] == "properties.active_flag_versions"
            for error in result["errors"]
        )

    def test_valid_web_vital_event(self):
        event = web_vital_event()
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_web_vital_rejects_noncanonical_rating(self):
        event = web_vital_event()
        event["properties"]["rating"] = "needs-improvement"

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "properties.rating" for error in result["errors"])

    def test_feature_flag_exposure_rejects_alias_properties(self):
        event = feature_flag_exposure_event()
        properties = event["properties"]
        properties.pop("flag_key")
        properties["flagKey"] = "checkout-gate"

        result = validate_single_event(event)

        assert result["valid"] is False
        fields = {error["field"] for error in result["errors"]}
        assert "properties.flag_key" in fields
        assert "properties.flagKey" in fields

    def test_feature_flag_exposure_rejects_top_level_alias_identifiers(self):
        event = feature_flag_exposure_event()
        event.pop("user_id")
        event.pop("anonymous_id")
        event["anonymousId"] = "anon_42"

        result = validate_single_event(event)

        assert result["valid"] is False
        fields = {error["field"] for error in result["errors"]}
        assert "user_id" in fields
        assert "anonymousId" in fields

    def test_feature_flag_exposure_rejects_unknown_envelope_fields(self):
        event = feature_flag_exposure_event()
        event["extra_field"] = "extra"

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "extra_field" for error in result["errors"])

    def test_feature_flag_exposure_requires_session_metadata(self):
        event = feature_flag_exposure_event()
        event.pop("session_id")

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "session_id" for error in result["errors"])

    def test_feature_flag_exposure_rejects_not_found_reason(self):
        event = feature_flag_exposure_event()
        event["properties"]["reason"] = "not_found"

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "properties.reason" for error in result["errors"])

    def test_feature_flag_exposure_rejects_boolean_value_property(self):
        event = feature_flag_exposure_event()
        event["properties"]["value"] = True

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "properties.value" for error in result["errors"])

    def test_feature_flag_exposure_rejects_legacy_bucket_property(self):
        event = feature_flag_exposure_event()
        properties = event["properties"]
        properties.pop("rollout_bucket")
        properties["bucket"] = 7.31

        result = validate_single_event(event)

        assert result["valid"] is False
        fields = {error["field"] for error in result["errors"]}
        assert "properties.rollout_bucket" in fields
        assert "properties.bucket" in fields

    def test_feature_flag_exposure_requires_canonical_metadata(self):
        event = feature_flag_exposure_event()
        event["properties"].pop("source")
        event["properties"].pop("page")
        event["properties"].pop("component")

        result = validate_single_event(event)

        assert result["valid"] is False
        fields = {error["field"] for error in result["errors"]}
        assert "properties.source" in fields
        assert "properties.page" in fields
        assert "properties.component" in fields

    def test_feature_flag_exposure_allows_empty_page_and_component(self):
        event = feature_flag_exposure_event()
        event["properties"]["page"] = ""
        event["properties"]["component"] = ""

        result = validate_single_event(event)

        assert result["valid"] is True

    def test_feature_flag_exposure_requires_non_empty_variant(self):
        event = feature_flag_exposure_event()
        event["properties"]["variant"] = ""

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "properties.variant" for error in result["errors"])

    def test_feature_flag_exposure_requires_track_type(self):
        event = feature_flag_exposure_event()
        event["type"] = "page"

        result = validate_single_event(event)

        assert result["valid"] is False
        assert any(error["field"] == "type" for error in result["errors"])

    def test_valid_identify_event(self):
        """ValidIdentifyEvent"""
        event = canonical_event(
            "identify",
            "identify",
            user_id="usr_42",
            traits={"name": "Alice", "plan": "pro"},
        )
        event.pop("anonymous_id")
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_valid_page_event(self):
        """ValidPageEvent"""
        event = canonical_event(
            "page",
            "page",
            anonymous_id="anon_xyz",
            properties={"url": "/pricing", "title": "Pricing"},
        )
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_valid_group_event(self):
        """ValidGroupEvent"""
        event = canonical_event(
            "group",
            "group",
            user_id="usr_1",
            group_id="group_1",
            traits={"company": "Acme Inc"},
        )
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_event_with_only_anonymous_id(self):
        """EventWithOnlyAnonymousId"""
        event = canonical_event("page_view", anonymous_id="anon_abc")
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_event_rejects_camel_case_anonymous_id(self):
        event = canonical_event("page_view")
        event.pop("anonymous_id")
        event["anonymousId"] = "anon_abc"
        result = validate_single_event(event)
        assert result["valid"] is False
        assert any(error["field"] == "anonymousId" for error in result["errors"])

    def test_event_missing_name_and_type(self):
        """EventMissingNameAndType"""
        event = {"user_id": "usr_1", "properties": {"key": "val"}}
        result = validate_single_event(event)
        assert result["valid"] is False
        found = any(e["field"] == "event" for e in result["errors"])
        assert found

    def test_event_missing_identifier(self):
        """EventMissingIdentifier"""
        event = {"event": "test_event", "type": "track"}
        result = validate_single_event(event)
        assert result["valid"] is False
        found = any(e["field"] == "user_id" for e in result["errors"])
        assert found

    def test_event_empty_user_id_treated_as_missing(self):
        """EventEmptyUserIdTreatedAsMissing"""
        event = {"event": "test", "user_id": ""}
        result = validate_single_event(event)
        assert result["valid"] is False

    def test_event_empty_name(self):
        """EventEmptyName"""
        event = {"event": "", "user_id": "usr_1"}
        result = validate_single_event(event)
        assert result["valid"] is False

    def test_event_invalid_type(self):
        """EventInvalidType"""
        event = {"type": "banana", "event": "test", "user_id": "usr_1"}
        result = validate_single_event(event)
        assert result["valid"] is False
        found = any(e["field"] == "type" for e in result["errors"])
        assert found

    def test_event_all_valid_types(self):
        """EventAllValidTypes"""
        for event_type in ("track", "identify", "group", "page"):
            name = event_type if event_type != "track" else "custom"
            event = canonical_event(name, event_type, user_id="u1")
            if event_type == "group":
                event["group_id"] = "g1"
            result = validate_single_event(event)
            assert result["valid"] is True, f"Type '{event_type}' should be valid"

        for event_type in ("screen", "alias"):
            event = canonical_event("custom", event_type, user_id="u1")
            assert validate_single_event(event)["valid"] is False

    def test_event_properties_not_object(self):
        """EventPropertiesNotObject"""
        event = {
            "event": "test",
            "user_id": "usr_1",
            "properties": [1, 2, 3],
        }
        result = validate_single_event(event)
        assert result["valid"] is False

    def test_event_traits_not_object(self):
        """EventTraitsNotObject"""
        event = {
            "type": "identify",
            "user_id": "usr_1",
            "traits": "not_object",
        }
        result = validate_single_event(event)
        assert result["valid"] is False

    def test_event_context_not_object(self):
        """EventContextNotObject"""
        event = {
            "event": "test",
            "user_id": "usr_1",
            "context": 42,
        }
        result = validate_single_event(event)
        assert result["valid"] is False

    @pytest.mark.parametrize("field", ["library", "locale", "page"])
    def test_context_members_reject_explicit_null(self, field):
        event = canonical_event(context={field: None})
        result = validate_single_event(event)
        assert result["valid"] is False

    @pytest.mark.parametrize(
        "context",
        [
            {"screen": {"width": True, "height": 100}},
            {"viewport": {"width": "100", "height": 100}},
            {"page": {"url": 42, "title": "", "path": "/", "search": ""}},
        ],
    )
    def test_context_values_are_not_coerced(self, context):
        event = canonical_event(context=context)
        result = validate_single_event(event)
        assert result["valid"] is False

    @pytest.mark.parametrize(
        "field",
        [
            "user_id",
            "anonymous_id",
            "group_id",
            "properties",
            "traits",
            "session_id",
        ],
    )
    def test_optional_event_fields_reject_explicit_null(self, field):
        event = canonical_event(user_id="user-1")
        event[field] = None
        result = validate_single_event(event)
        assert result["valid"] is False

    def test_event_timestamp_not_string(self):
        """EventTimestampNotString"""
        event = {
            "event": "test",
            "user_id": "usr_1",
            "timestamp": 1234567890,
        }
        result = validate_single_event(event)
        assert result["valid"] is False

    @pytest.mark.parametrize(
        "timestamp",
        [
            "2026-07-13T12:00Z",
            "2026-07-13 12:00:00Z",
            "2026-07-13t12:00:00Z",
            "2026-07-13T12:00:00.1234567Z",
            "0999-01-01T00:00:00Z",
            "2026-02-30T12:00:00Z",
            "2026-07-13T12:00:00+00:00",
        ],
    )
    def test_event_timestamp_requires_canonical_rfc3339_utc(self, timestamp):
        event = canonical_event(timestamp=timestamp)
        result = validate_single_event(event)
        assert result["valid"] is False

    def test_event_not_an_object(self):
        """EventNotAnObject"""
        result = validate_single_event("just a string")
        assert result["valid"] is False

    def test_multiple_errors_collected(self):
        """MultipleErrorsCollected -- multiple validation failures in one event."""
        event = {
            "properties": "invalid",
            "traits": 42,
            "context": [1],
        }
        result = validate_single_event(event)
        assert result["valid"] is False
        # Should have errors for: missing event/type, missing user_id,
        # invalid properties, invalid traits, invalid context
        assert len(result["errors"]) >= 4

    def test_property_key_exceeds_max_length(self):
        """Property keys longer than 256 chars should be rejected."""
        long_key = "k" * 257
        event = {
            "event": "test",
            "user_id": "u1",
            "properties": {long_key: "value"},
        }
        result = validate_single_event(event)
        assert result["valid"] is False
        found = any("Property key exceeds" in e["message"] for e in result["errors"])
        assert found

    def test_string_property_value_exceeds_max_length(self):
        """String property values longer than 8192 chars should be rejected."""
        long_val = "v" * 8193
        event = {
            "event": "test",
            "user_id": "u1",
            "properties": {"key": long_val},
        }
        result = validate_single_event(event)
        assert result["valid"] is False
        found = any(
            "String property value exceeds" in e["message"]
            for e in result["errors"]
        )
        assert found

    def test_property_key_at_max_length_is_valid(self):
        """Property key of exactly 256 chars should be accepted."""
        key = "k" * 256
        event = canonical_event("test", user_id="u1", properties={key: "value"})
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_string_property_value_at_max_length_is_valid(self):
        """String property value of exactly 8192 chars should be accepted."""
        val = "v" * 8192
        event = canonical_event("test", user_id="u1", properties={"key": val})
        result = validate_single_event(event)
        assert result["valid"] is True

    def test_event_name_exceeds_max_length(self):
        """Event names longer than 256 chars should be rejected."""
        long_name = "e" * 257
        event = {"event": long_name, "user_id": "u1"}
        result = validate_single_event(event)
        assert result["valid"] is False
        found = any(
            "Event name exceeds" in e["message"] for e in result["errors"]
        )
        assert found

    def test_event_name_at_max_length_is_valid(self):
        """Event name of exactly 256 chars should be accepted."""
        name = "e" * 256
        event = canonical_event(name, user_id="u1")
        result = validate_single_event(event)
        assert result["valid"] is True


def feature_flag_exposure_event():
    return {
        "event": "$feature_flag_exposure",
        "type": "track",
        "user_id": "usr_42",
        "anonymous_id": "anon_42",
        "session_id": "sess_42",
        "message_id": "msg_42",
        "timestamp": "2026-05-26T02:26:53.455Z",
        "context": {},
        "properties": {
            "flag_key": "checkout-gate",
            "variant": "treatment",
            "reason": "fallthrough",
            "rule_id": None,
            "rollout_bucket": 7.31,
            "variant_bucket": 74.2,
            "rollout_percentage": 100,
            "bucket_by": "user_id",
            "config_version": 3,
            "source": "initial_fetch",
            "page": "/checkout",
            "component": "CheckoutPage",
        },
    }


def frontend_error_event():
    return {
        "event": "$frontend_error",
        "type": "track",
        "user_id": "usr_42",
        "anonymous_id": "anon_42",
        "session_id": "sess_42",
        "message_id": "msg_42",
        "timestamp": "2026-05-26T02:26:53.455Z",
        "context": {},
        "properties": {
            "error_type": "javascript_error",
            "message": "Checkout exploded",
            "page": "/checkout",
            "component": "",
            "slot_id": "",
            "source": "checkout.js",
            "line": 12,
            "column": 4,
            "stack": "Error: Checkout exploded",
            "active_flags": {"checkout-gate": "treatment"},
            "active_flag_versions": {"checkout-gate": 3},
        },
    }


def web_vital_event():
    return {
        "event": "$web_vital",
        "type": "track",
        "user_id": "usr_42",
        "anonymous_id": "anon_42",
        "session_id": "sess_42",
        "message_id": "msg_42",
        "timestamp": "2026-05-26T02:26:53.455Z",
        "context": {},
        "properties": {
            "metric": "LCP",
            "value": 2410.5,
            "rating": "needs_improvement",
            "delta": 2410.5,
            "id": "vital_1",
            "navigation_type": "navigate",
            "page": "/checkout",
            "active_flags": {"checkout-gate": "treatment"},
            "active_flag_versions": {"checkout-gate": 3},
        },
    }
