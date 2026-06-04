"""Registry behaviour: registration, lookup, duplicates, listing."""

from __future__ import annotations

import pytest

from etl import (
    BaseTransform,
    get_transform,
    is_registered,
    list_transforms,
    register_transform,
    registered_transforms,
)
from etl.context import EtlContext, Row
from etl.envelope import CanonicalEnvelope


def test_builtins_are_registered():
    schemas = set(list_transforms())
    assert {"track@1", "page@1", "identify@1"} <= schemas
    assert {"flag_eval@1", "exposure@1", "agent_action@1", "personalization@1"} <= schemas
    assert "partner.shipments.csv@1" in schemas


def test_get_transform_returns_memoised_instance():
    a = get_transform("track@1")
    b = get_transform("track@1")
    assert a is b  # memoised singleton
    assert a.target_table == "events_v2"


def test_get_unknown_schema_raises():
    with pytest.raises(KeyError):
        get_transform("does.not.exist@9")


def test_is_registered():
    assert is_registered("track@1")
    assert not is_registered("nope@1")


def test_register_requires_schema():
    with pytest.raises(ValueError, match="non-empty 'schema'"):

        @register_transform
        class NoSchema(BaseTransform):
            def build_row(self, env: CanonicalEnvelope, ctx: EtlContext, enrichment) -> Row:
                return {}


def test_duplicate_schema_rejected():
    with pytest.raises(ValueError, match="Duplicate transform schema"):

        @register_transform
        class Dupe(BaseTransform):
            schema = "track@1"

            def build_row(self, env: CanonicalEnvelope, ctx: EtlContext, enrichment) -> Row:
                return {}


def test_re_registering_same_class_is_idempotent():
    cls = registered_transforms()["track@1"]
    # Decorating the already-registered class again must not raise.
    assert register_transform(cls) is cls


def test_list_is_grouped_by_target_table():
    listed = list_transforms()
    tables = [registered_transforms()[s].target_table for s in listed]
    # Once a table appears, it should not reappear later (i.e. grouped).
    seen: set[str] = set()
    last = None
    for t in tables:
        if t != last:
            assert t not in seen, f"target table {t} is not contiguous"
            seen.add(t)
            last = t
