"""BaseTransform lifecycle: happy path, decode failure, and error isolation."""

from __future__ import annotations

from typing import Any

from etl import BaseTransform, EtlContext, register_transform
from etl.context import Row
from etl.envelope import CanonicalEnvelope
from tests.conftest import make_envelope


@register_transform
class _DummyTransform(BaseTransform):
    """Test-only transform exercising the base lifecycle."""

    schema = "dummy.test@1"
    target_table = "dummy_table"
    enrichers = ("geo",)

    def validate(self, env: CanonicalEnvelope, ctx: EtlContext) -> None:
        if env.payload.get("reject"):
            raise ValueError("explicitly rejected")

    def build_row(self, env: CanonicalEnvelope, ctx: EtlContext, enrichment: dict[str, Any]) -> Row:
        row = self.envelope_columns(env, ctx)
        row["country"] = enrichment.get("country", "")
        return row


def test_happy_path_produces_row(ctx):
    raw = make_envelope("dummy.test@1", {"context": {"geo": {"country": "fr"}}})
    result = _DummyTransform().process(raw, ctx)
    assert result.ok
    assert result.target == "dummy_table"
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["_project_id"] == 42
    assert row["_schema"] == "dummy.test@1"
    assert row["_received_at"] == ctx.received_at
    assert row["country"] == "FR"  # enrichment ran


def test_correlation_id_defaults_to_zero_uuid(ctx):
    raw = make_envelope("dummy.test@1", {})
    row = _DummyTransform().process(raw, ctx).rows[0]
    assert row["_correlation_id"] == "00000000-0000-0000-0000-000000000000"


def test_decode_failure_goes_to_dlq(ctx):
    # Unknown top-level key -> envelope validation (extra="forbid") fails.
    raw = make_envelope("dummy.test@1", {})
    raw["_unexpected"] = "boom"
    result = _DummyTransform().process(raw, ctx)
    assert not result.ok
    assert result.dlq is not None
    assert result.dlq.project_id == 42
    assert result.dlq.table == "events_dlq_v2"
    assert "_unexpected" in result.dlq.raw_payload


def test_validate_failure_goes_to_dlq(ctx):
    raw = make_envelope("dummy.test@1", {"reject": True})
    result = _DummyTransform().process(raw, ctx)
    assert not result.ok
    assert "explicitly rejected" in result.dlq.error


def test_build_row_can_return_multiple_rows(ctx):
    @register_transform
    class _FanOut(BaseTransform):
        schema = "fanout.test@1"
        target_table = "t"

        def build_row(self, env, ctx, enrichment):
            base = self.envelope_columns(env, ctx)
            return [dict(base, n=i) for i in range(3)]

    raw = make_envelope("fanout.test@1", {})
    result = _FanOut().process(raw, ctx)
    assert result.ok
    assert [r["n"] for r in result.rows] == [0, 1, 2]


def test_dlq_raw_payload_is_serialisable(ctx):
    raw = make_envelope("dummy.test@1", {"reject": True})
    result = _DummyTransform().process(raw, ctx)
    # raw_payload is a JSON string the DLQ table can store verbatim.
    assert isinstance(result.dlq.raw_payload, str)
    assert result.dlq.raw_payload.startswith("{")
