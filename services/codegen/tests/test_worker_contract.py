"""Tests for the controller-to-Codegen-worker stdin trust boundary."""

from __future__ import annotations

import io
import json

import pytest

from app.editor.base import EditRequest
from app.editor.worker_contract import (
    CODEGEN_PREPARATION_REQUEST_SCHEMA_VERSION,
    CODEGEN_WORKER_REQUEST_SCHEMA_VERSION,
    MAX_CODEGEN_PREPARATION_REQUEST_BYTES,
    MAX_CODEGEN_WORKER_REQUEST_BYTES,
    CodegenWorkerRequestError,
    decode_codegen_preparation_request,
    decode_codegen_worker_request,
    encode_codegen_preparation_request,
    encode_codegen_worker_request,
    read_codegen_preparation_request,
    read_codegen_worker_request,
)
from tests.preparation_fakes import repository_preparation


def _request(**overrides: object) -> EditRequest:
    values: dict[str, object] = {
        "repo": "acme/widgets",
        "project_scope": "project-123",
        "base_branch": "main",
        "branch": "apdl/change",
        "token": "ghs_read_only",
        "title": "Make a bounded change",
        "spec": "Do not expose this task text through process metadata.",
        "constraints": ["keep tests green"],
        "test_cmd": "python -m pytest -q",
        "risk_level": "high",
    }
    values.update(overrides)
    request = EditRequest(**values)
    request.repository_preparation = repository_preparation(request)
    return request


def test_preparation_request_is_one_canonical_versioned_envelope():
    request = _request()
    encoded = encode_codegen_preparation_request(request)
    payload = json.loads(encoded)

    assert payload["schema_version"] == CODEGEN_PREPARATION_REQUEST_SCHEMA_VERSION
    assert "repository_preparation" not in payload
    assert decode_codegen_preparation_request(encoded).repository == "acme/widgets"


def test_preparation_request_schema_and_reader_are_strict_and_bounded():
    encoded = encode_codegen_preparation_request(_request())
    payload = json.loads(encoded)
    payload["legacy_preflight"] = {}

    with pytest.raises(CodegenWorkerRequestError, match="strict schema"):
        decode_codegen_preparation_request(json.dumps(payload).encode("utf-8"))
    with pytest.raises(CodegenWorkerRequestError, match="input limit"):
        decode_codegen_preparation_request(
            b"x" * (MAX_CODEGEN_PREPARATION_REQUEST_BYTES + 1)
        )

    class RecordingStream(io.BytesIO):
        requested: int | None = None

        def read(self, size: int = -1) -> bytes:
            self.requested = size
            return super().read(size)

    stream = RecordingStream(encoded)
    assert read_codegen_preparation_request(stream).repository == "acme/widgets"
    assert stream.requested == MAX_CODEGEN_PREPARATION_REQUEST_BYTES + 1


def test_worker_request_is_one_canonical_versioned_envelope():
    encoded = encode_codegen_worker_request(_request())
    payload = json.loads(encoded)

    assert payload["schema_version"] == CODEGEN_WORKER_REQUEST_SCHEMA_VERSION
    assert set(payload) == {
        "schema_version",
        "read_token",
        "repository",
        "project_scope",
        "base_branch",
        "branch",
        "title",
        "spec",
        "constraints",
        "test_cmd",
        "safety_policy",
        "safety_policy_sha256",
        "revert_sha",
        "existing_branch",
        "expected_head_sha",
        "risk_level",
        "requirement_ledger",
        "runtime_acceptance_plan",
        "runtime_acceptance_policy",
        "repository_preparation",
    }
    decoded = decode_codegen_worker_request(encoded)
    reconstructed = decoded.to_edit_request()
    assert reconstructed.repo == "acme/widgets"
    assert reconstructed.token == "ghs_read_only"
    assert reconstructed.spec == _request().spec
    assert reconstructed.safety_policy == _request().safety_policy


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"schema_version": "codegen_worker_request@3"}),
        lambda value: value.update({"legacy_spec": value["spec"]}),
        lambda value: value.pop("spec"),
        lambda value: value.update({"existing_branch": "false"}),
    ],
    ids=["unsupported-version", "unknown-field", "missing-field", "wrong-type"],
)
def test_worker_request_rejects_noncanonical_schemas(mutation):
    payload = json.loads(encode_codegen_worker_request(_request()))
    mutation(payload)

    with pytest.raises(CodegenWorkerRequestError, match="strict schema"):
        decode_codegen_worker_request(json.dumps(payload).encode("utf-8"))


def test_worker_request_rejects_invalid_encoding_and_json_framing():
    with pytest.raises(CodegenWorkerRequestError, match="UTF-8"):
        decode_codegen_worker_request(b"\xff")
    with pytest.raises(CodegenWorkerRequestError, match="strict JSON object"):
        decode_codegen_worker_request(b'{}\n{"schema_version":"second"}')
    with pytest.raises(CodegenWorkerRequestError, match="strict JSON object"):
        decode_codegen_worker_request(b'{"schema_version":"a","schema_version":"b"}')


def test_worker_request_rejects_oversized_input_before_json_decoding():
    with pytest.raises(CodegenWorkerRequestError, match="input limit"):
        decode_codegen_worker_request(b"x" * (MAX_CODEGEN_WORKER_REQUEST_BYTES + 1))


def test_worker_request_reader_performs_one_bounded_read():
    class RecordingStream(io.BytesIO):
        requested: int | None = None

        def read(self, size: int = -1) -> bytes:
            self.requested = size
            return super().read(size)

    stream = RecordingStream(encode_codegen_worker_request(_request()))

    assert read_codegen_worker_request(stream).repository == "acme/widgets"
    assert stream.requested == MAX_CODEGEN_WORKER_REQUEST_BYTES + 1


def test_worker_request_rejects_preparation_identity_substitution():
    payload = json.loads(encode_codegen_worker_request(_request()))
    payload["repository_preparation"]["attestation"]["repository"] = "other/widgets"

    with pytest.raises(CodegenWorkerRequestError, match="strict schema"):
        decode_codegen_worker_request(json.dumps(payload).encode("utf-8"))


def test_worker_request_rejects_preparation_from_a_different_source_request():
    request = _request()
    preparation = request.repository_preparation
    assert preparation is not None
    request.spec = "A substituted task"
    request.repository_preparation = preparation

    with pytest.raises(CodegenWorkerRequestError, match="strict schema"):
        encode_codegen_worker_request(request)


def test_preparation_binding_excludes_only_the_rotating_read_token():
    request = _request()
    preparation = request.repository_preparation
    assert preparation is not None
    request.token = "ghs_rotated_read_only"
    request.repository_preparation = preparation

    decoded = decode_codegen_worker_request(encode_codegen_worker_request(request))

    assert decoded.read_token == "ghs_rotated_read_only"
    assert decoded.repository_preparation.request_sha256 == preparation.request_sha256


def test_preparation_provenance_hashes_non_ascii_requirement_text():
    request = _request(spec="Préserve les données déjà validées.")

    decoded = decode_codegen_worker_request(encode_codegen_worker_request(request))

    assert decoded.spec == "Préserve les données déjà validées."
