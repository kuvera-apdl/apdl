"""EtlPipeline dispatch, DLQ routing, batching, and stats."""

from __future__ import annotations

from etl import BatchingLoader, CollectingLoader, EtlPipeline
from tests.conftest import make_envelope


def test_dispatch_routes_to_target_table(ctx):
    loader = CollectingLoader()
    pipe = EtlPipeline(loader)
    pipe.process_record(make_envelope("track@1", {"anonymous_id": "a", "event": "e"}), ctx)
    pipe.process_record(make_envelope("flag_eval@1", {"user_id": "u", "flag_key": "f"}), ctx)
    assert loader.count("events_v2") == 1
    assert loader.count("decisions_v2") == 1


def test_unrouted_schema_goes_to_dlq(ctx):
    loader = CollectingLoader()
    pipe = EtlPipeline(loader)
    result = pipe.process_record(make_envelope("ghost@9", {}), ctx)
    assert not result.ok
    assert loader.count("events_dlq_v2") == 1
    assert pipe.stats["unrouted"] == 1


def test_missing_schema_key_goes_to_dlq(ctx):
    loader = CollectingLoader()
    pipe = EtlPipeline(loader)
    result = pipe.process_record({"_project_id": "project42", "payload": {}}, ctx)
    assert not result.ok
    assert pipe.stats["unrouted"] == 1


def test_transform_failure_routes_to_dlq_loader(ctx):
    rows = CollectingLoader()
    dlq = CollectingLoader()
    pipe = EtlPipeline(rows, dlq_loader=dlq)
    # track@1 with no event -> transform-level DLQ
    pipe.process_record(make_envelope("track@1", {"anonymous_id": "a"}), ctx)
    assert rows.total() == 0
    assert dlq.count("events_dlq_v2") == 1


def test_process_batch_and_stats(ctx):
    loader = CollectingLoader()
    pipe = EtlPipeline(loader)
    records = [
        make_envelope("track@1", {"anonymous_id": "a", "event": "e1"}),
        make_envelope("track@1", {"anonymous_id": "a"}),          # bad -> dlq
        make_envelope("flag_eval@1", {"user_id": "u", "flag_key": "f"}),
        make_envelope("ghost@9", {}),                              # unrouted -> dlq
    ]
    results = pipe.process_batch(records, ctx)
    assert len(results) == 4
    assert pipe.stats == {"processed": 4, "rows": 2, "dlq": 2, "unrouted": 1}


def test_batching_loader_flushes_on_threshold():
    flushed: list[tuple[str, int]] = []
    loader = BatchingLoader(lambda target, rows: flushed.append((target, len(rows))), batch_size=2)
    loader.load("events_v2", [{"a": 1}])
    assert flushed == []           # below threshold
    loader.load("events_v2", [{"a": 2}])
    assert flushed == [("events_v2", 2)]  # threshold reached -> flush


def test_batching_loader_manual_flush_drains_remainder():
    flushed: list[tuple[str, int]] = []
    loader = BatchingLoader(lambda target, rows: flushed.append((target, len(rows))), batch_size=10)
    loader.load("events_v2", [{"a": 1}, {"a": 2}])
    assert loader.pending("events_v2") == 2
    loader.flush()
    assert flushed == [("events_v2", 2)]
    assert loader.pending("events_v2") == 0
