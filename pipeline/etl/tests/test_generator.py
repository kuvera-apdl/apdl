"""The scaffolding generator: name derivation and rendered output."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "new_transform.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("new_transform", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


@pytest.mark.parametrize(
    "schema,expected_module,expected_class",
    [
        ("track@1", "track", "TrackTransform"),
        ("refund.issued@1", "refund_issued", "RefundIssuedTransform"),
        ("edi.x12.850@1", "edi_x12_850", "EdiX12850Transform"),
        ("partner.shipments.csv@2", "partner_shipments_csv", "PartnerShipmentsCsvTransform"),
    ],
)
def test_name_derivation(schema, expected_module, expected_class):
    assert gen.module_name(schema) == expected_module
    assert gen.class_name(schema) == expected_class


def test_render_produces_registerable_class():
    args = argparse.Namespace(
        schema="refund.issued@1",
        description="A refund was issued",
        target_table="events_v2",
        enrichers=["device", "geo"],
        validate=True,
    )
    out = gen.render(args)
    assert "@register_transform" in out
    assert "class RefundIssuedTransform(BaseTransform):" in out
    assert 'schema = "refund.issued@1"' in out
    assert "enrichers = ('device', 'geo')" in out
    assert "def validate(" in out
    assert "def build_row(" in out


def test_render_without_validate_omits_hook():
    args = argparse.Namespace(
        schema="thing.happened@1",
        description="d",
        target_table="events_v2",
        enrichers=[],
        validate=False,
    )
    out = gen.render(args)
    assert "def validate(" not in out
    assert "enrichers = ()" in out
