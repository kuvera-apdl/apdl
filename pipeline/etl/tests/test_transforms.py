"""Built-in transforms produce the right rows for each canonical table."""

from __future__ import annotations

import json

from etl import get_transform
from etl.transforms.decisions import DECISIONS_V2_COLUMNS
from etl.transforms.events import EVENTS_V2_COLUMNS
from etl.transforms.feeds import FEEDS_V2_COLUMNS
from tests.conftest import make_envelope


# ---------------------------------------------------------------- events ----

def test_track_promotes_event_name_and_keeps_properties(ctx):
    raw = make_envelope("track@1", {
        "anonymous_id": "anon-1",
        "user_id": "u1",
        "event": "checkout_started",
        "properties": {"revenue": 9.99},
    })
    row = get_transform("track@1").process(raw, ctx).rows[0]
    assert row["event_name"] == "checkout_started"
    assert row["user_id"] == "u1"
    assert json.loads(row["properties"]) == {"revenue": 9.99}
    assert set(row) == set(EVENTS_V2_COLUMNS)


def test_track_without_event_name_is_dlq(ctx):
    raw = make_envelope("track@1", {"anonymous_id": "a"})
    result = get_transform("track@1").process(raw, ctx)
    assert not result.ok
    assert "event" in result.dlq.error


def test_page_folds_name_and_category_into_properties(ctx):
    raw = make_envelope("page@1", {
        "anonymous_id": "a",
        "name": "Pricing",
        "category": "Marketing",
        "context": {"page": {"url": "https://x/pricing", "referrer": "https://g"}},
    })
    row = get_transform("page@1").process(raw, ctx).rows[0]
    assert row["event_name"] == "page"
    props = json.loads(row["properties"])
    assert props["name"] == "Pricing" and props["category"] == "Marketing"
    assert row["page_url"] == "https://x/pricing"
    assert row["referrer"] == "https://g"


def test_identify_routes_traits_to_traits_column(ctx):
    raw = make_envelope("identify@1", {
        "anonymous_id": "a",
        "user_id": "u1",
        "traits": {"plan": "pro", "email": "x@y.z"},
    })
    row = get_transform("identify@1").process(raw, ctx).rows[0]
    assert row["event_name"] == "identify"
    assert json.loads(row["traits"]) == {"email": "x@y.z", "plan": "pro"}


def test_group_and_alias_fold_ids(ctx):
    g = get_transform("group@1").process(
        make_envelope("group@1", {"anonymous_id": "a", "group_id": "acct-9"}), ctx
    ).rows[0]
    assert json.loads(g["properties"])["group_id"] == "acct-9"

    al = get_transform("alias@1").process(
        make_envelope("alias@1", {"anonymous_id": "a", "previous_id": "old-7"}), ctx
    ).rows[0]
    assert json.loads(al["properties"])["previous_id"] == "old-7"


# ------------------------------------------------------------- decisions ----

def test_flag_eval_promotes_sparse_columns(ctx):
    raw = make_envelope("flag_eval@1", {
        "user_id": "u1",
        "flag_key": "new_ui",
        "variant": "on",
        "reason": "rollout",
        "rule_id": "r-12",
        "rollout_bucket": 4211,
    })
    row = get_transform("flag_eval@1").process(raw, ctx).rows[0]
    assert row["flag_key"] == "new_ui"
    assert row["variant"] == "on"
    assert row["rollout_bucket"] == 4211
    # columns not relevant to this schema keep their defaults
    assert row["action_type"] == ""
    assert row["run_id"] == "00000000-0000-0000-0000-000000000000"
    assert set(row) == set(DECISIONS_V2_COLUMNS)


def test_agent_action_promotes_run_id_and_safety(ctx):
    raw = make_envelope("agent_action@1", {
        "user_id": "u1",
        "action_type": "propose_experiment",
        "approval_status": "pending",
        "run_id": "11111111-1111-1111-1111-111111111111",
        "safety_result": {"passed": True, "risk": "low"},
    })
    row = get_transform("agent_action@1").process(raw, ctx).rows[0]
    assert row["action_type"] == "propose_experiment"
    assert row["approval_status"] == "pending"
    assert row["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert json.loads(row["safety_result"])["risk"] == "low"


def test_all_decision_schemas_share_identical_columns(ctx):
    for schema in ("flag_eval@1", "exposure@1", "agent_action@1", "personalization@1"):
        raw = make_envelope(schema, {"user_id": "u1"})
        row = get_transform(schema).process(raw, ctx).rows[0]
        assert set(row) == set(DECISIONS_V2_COLUMNS), schema


# ----------------------------------------------------------------- feeds ----

def test_feed_reads_source_pointer_from_ctx_extra():
    from etl import EtlContext
    from tests.conftest import RECEIVED_AT

    ctx = EtlContext(
        project_id="project7",
        received_at=RECEIVED_AT,
        source="edi-adapter@1.0",
        extra={"source_uri": "s3://bucket/x.csv", "source_sha256": "a" * 64, "source_bytes": 2048},
    )
    raw = make_envelope("partner.shipments.csv@1", {
        "sender_id": "ACME",
        "receiver_id": "APDL",
        "control_number": "000123",
        "parse_warnings": ["col 9 empty"],
    }, _project_id="project7")
    row = get_transform("partner.shipments.csv@1").process(raw, ctx).rows[0]
    assert row["source_uri"] == "s3://bucket/x.csv"
    assert row["source_bytes"] == 2048
    assert row["sender_id"] == "ACME"
    assert row["parse_warnings"] == ["col 9 empty"]
    assert set(row) == set(FEEDS_V2_COLUMNS)
