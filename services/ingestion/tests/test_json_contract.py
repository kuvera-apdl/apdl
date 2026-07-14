import json

import pytest

from app.middleware.rate_limit import request_cost
from app.validation.json_contract import (
    MAX_CONTAINER_ITEMS,
    MAX_EVENT_BYTES,
    MAX_JSON_DEPTH,
    CanonicalJSONError,
    parse_canonical_json,
    validate_event_json_bounds,
)


def test_parser_rejects_duplicate_keys():
    with pytest.raises(CanonicalJSONError, match="Duplicate"):
        parse_canonical_json(b'{"event":"one","event":"two"}')


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parser_rejects_nonfinite_numbers(constant):
    with pytest.raises(CanonicalJSONError, match="Non-finite"):
        parse_canonical_json(f'{{"value":{constant}}}'.encode())


def test_bounds_reject_excessive_depth():
    value: object = "leaf"
    for _ in range(MAX_JSON_DEPTH + 1):
        value = {"nested": value}
    with pytest.raises(CanonicalJSONError, match="depth"):
        validate_event_json_bounds(value)


def test_bounds_reject_excessive_container_cardinality():
    value = {f"key_{index}": index for index in range(MAX_CONTAINER_ITEMS + 1)}
    with pytest.raises(CanonicalJSONError, match="object fields"):
        validate_event_json_bounds(value)


def test_bounds_reject_oversized_event():
    value = {"value": "x" * MAX_EVENT_BYTES}
    with pytest.raises(CanonicalJSONError, match="serialized size"):
        validate_event_json_bounds(value)


def test_bounds_reject_lone_surrogate_that_cannot_encode_as_utf8():
    with pytest.raises(CanonicalJSONError, match="non-JSON value"):
        validate_event_json_bounds({"value": "\ud800"})


def test_rate_cost_charges_events_and_rounded_kibibytes():
    assert request_cost(3, 1) == 4
    assert request_cost(3, 1024) == 4
    assert request_cost(3, 1025) == 5


def test_canonical_payload_round_trips_without_changes():
    payload = {"events": [{"event": "signup", "properties": {"plan": "pro"}}]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    assert parse_canonical_json(raw) == payload
