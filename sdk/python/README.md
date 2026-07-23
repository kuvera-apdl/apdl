# apdl-sdk (Python)

Server-side Python client for the **Autonomous Product Development Loop** platform.
Capture analytics events and evaluate variant feature flags locally — with the
same FNV-1a bucketing as the JavaScript SDK and the config service, so a user
buckets identically no matter where a flag is evaluated.

- 🧵 Non-blocking event capture via a background batching/flush thread
- 🚩 Local variant flag evaluation (no network round-trip on the hot path)
- 🔁 Background flag-config refresh from the config service
- 🔬 Fully-explained results (`reason`, `variant`, `rollout_bucket`, `variant_bucket`, `rule_id`, …) and automatic exposure logging
- ✅ Pydantic v2 models throughout; ships with `py.typed`

Requires Python 3.12+. Runtime dependencies: `httpx`, `pydantic`.

## Install

```bash
pip install apdl-sdk
# or, from the monorepo:
cd sdk/python && uv pip install -e ".[dev]"
```

## Quick start

```python
from apdl import APDL

client = APDL.init(
    api_key="proj_<project>_<secret>",  # secret: 16-128 alphanumeric chars
    endpoint="https://apdl.example.com",  # your APDL gateway origin
)
# The key format is validated at init (same regex as the ingestion/config
# services); a malformed key raises immediately instead of 401-ing on first send.
# client.project_id  -> "<project>" (client hint; servers verify authority)

# Track events (identity is explicit per call — servers handle many users)
client.track("order_completed", {"total": 42.0}, user_id="u_123")
client.identify("u_123", {"plan": "pro"})  # trait update only
client.identify("u_123", anonymous_id="anon_123")  # canonical alias assertion
client.group("org_42", {"name": "Acme"}, user_id="u_123")
client.page("/checkout", user_id="u_123")

# Evaluate a variant feature flag locally
if client.get_variant("new-checkout", user_id="u_123") == "treatment":
    ...

report = client.shutdown()  # drains or returns every pending event, then stops
if not report.complete:
    persist_for_replay(report.undelivered_events)
```

Event properties and traits must be canonical JSON: strings, finite numbers,
booleans, nulls, arrays, and string-keyed objects. Cycles, Python-only values,
excessive nesting/cardinality, and events over 64 KiB raise `ValueError`
synchronously and never enter the background queue. Delivery batches stay
within the ingestion service's 512 KiB request limit. Event timestamps may be
at most seven days old and at most five minutes ahead of the SDK clock,
matching ingestion; out-of-window time is rejected rather than rewritten.
HTTP 408/425/429,
5xx, and network failures remain queued with their original `message_id`;
permanent rejections are discarded so they cannot block later valid events.
Optional event and context fields use omission as their only absent wire form;
explicit `null`, unknown context fields, and context aliases are rejected.

An `identify` call with both `user_id` and `anonymous_id` is the only
anonymous-to-user alias assertion. Omitting `anonymous_id` keeps the call as a
trait update. Accepted assertions are tenant-bound and irreversible; there is
no alias event, `previous_id`, or unmerge field. They become query-visible after
the ingestion writer durably stores them and then apply retroactively to
retained events. Conflicting claims fail closed as separate actors.

The Python server SDK accepts only confidential `proj_<project>_<secret>`
credentials. Browser-safe `client_...` credentials belong to the JavaScript
SDK and are intentionally rejected here.

`endpoint` is always required: the SDK has no hosted-service fallback. Pass
the HTTP(S) origin of the gateway you operate, without credentials, a path,
query parameters, or a fragment.

Or as a context manager (auto-shutdown):

```python
with APDL.init(
    api_key="proj_demo_0123456789abcdef",
    endpoint="http://localhost:8000",
) as client:
    client.track("signup", user_id="u_999")
```

## Configuration

Pass keyword args to `APDL.init(...)` or build an `APDLConfig`:

```python
from apdl import APDL, APDLConfig

client = APDL.init(APDLConfig(
    api_key="proj_demo_0123456789abcdef",
    endpoint="https://apdl.example.com",   # required gateway origin for events + flags
    batch_size=20,                         # 1..100
    flush_interval=3.0,                    # seconds between background flushes
    max_queue_size=1000,                   # new capture raises BufferError when full
    enable_flags=True,                     # fetch + poll flag configs
    flag_poll_interval=30.0,               # seconds between flag refreshes
    log_exposures=True,                    # emit $feature_flag_exposure events
    request_timeout=10.0,
    debug=False,
))
```

Configuration is strict: pass native strings, numbers, and booleans. Stringified
numbers or booleans and numeric values outside the documented bounds are rejected.

## Lifecycle and delivery reports

`flush()` and `shutdown()` return a `DeliveryReport` with `accepted`,
`permanently_rejected`, and `undelivered_events`. A normal `flush()` serializes
concurrent flush callers and completes one drain attempt. `shutdown()` first
rejects new tracking calls, interrupts retry backoff, waits for any in-flight
HTTP request to finish within `request_timeout`, makes at most one final attempt,
and closes the transport only after the queue worker has stopped.

Retryable events remain in the closed client's in-memory queue and are returned
as detached snapshots with their original `message_id`. Persist that snapshot
before process exit if it must survive a restart:

```python
report = client.shutdown()
if report.undelivered:
    save_json("apdl-undelivered.json", report.undelivered_events)
```

`shutdown()` is idempotent: concurrent or later callers receive the same report
without another send or transport close. `client.pending_events` continues to
report retained events, while `track`, `identify`, `group`, and `page` raise
`RuntimeError` after shutdown starts. If a context manager performed shutdown,
calling `client.shutdown()` once more retrieves the same report.

The queue never evicts an event it already accepted. When `max_queue_size` is
full, new tracking calls raise `BufferError` synchronously. If concurrent intake
uses space temporarily freed by an in-flight request, a retry preserves both
the original batch and the newer events even when this temporarily exceeds the
intake cap; further intake is rejected until the queue drains.

## Variant feature flags

Every flag is a set of weighted variants (a binary flag is `control`/`treatment`).
`get_variant` returns the assigned variant key — or `None` when the flag is
missing or its config is invalid; `get_variant_details` returns a
fully-explained `GateEvaluationResult`:

```python
variant = client.get_variant("new-checkout", user_id="u_123")
if variant == "treatment":
    ...

result = client.get_variant_details("new-checkout", user_id="u_123", attributes={"plan": "pro"})
print(result.variant, result.reason, result.rule_id, result.variant_bucket)
# treatment  rule_match  r_pro_users  73.4
```

`reason` is one of `not_found`, `invalid_config`, `disabled`, `error`,
`rule_match`, `rule_rollout`, `fallthrough`, `fallthrough_rollout`. Detail
fields that do not apply are `None` (never `""`/`0`).

Calling `get_variant`/`get_variant_details` automatically emits a deduplicated
`$feature_flag_exposure` event (disable per call with `log_exposure=False`, or
globally with `log_exposures=False`). Pass `page=`/`component=` to annotate the
exposure.

### Bulk evaluation

To bootstrap a downstream client (or render a server-side page) you often want
every flag at once for a single identity. `get_all_variants` evaluates all
cached flags in one call and returns `{key: variant}`; `get_all_variant_details`
returns the fully-explained `GateEvaluationResult` for each, ordered by key:

```python
variants = client.get_all_variants(user_id="u_123", attributes={"plan": "pro"})
# {"new-checkout": "treatment", "dark-mode": "control"}
```

Flags missing from the local cache are simply absent from the result. Bulk
evaluation **never** logs exposures — returning a snapshot is not the same as
exposing a user to each flag; call `get_variant` where a true exposure occurs.

React to config changes (e.g. to bust a local cache). The callback takes no
value, because the evaluated result depends on per-request context:

```python
unsubscribe = client.on_variant_change("new-checkout", lambda: my_cache.clear())
...
unsubscribe()
```

### Advanced: direct hashing

The bucketing primitives are exported for advanced use (and cross-SDK parity tests):

```python
from apdl import hash_bucket, percentage_bucket, is_in_rollout

hash_bucket("new-checkout", "salt", "u_123")          # -> uint32
percentage_bucket("new-checkout", "salt", "u_123")    # -> [0, 100)
is_in_rollout("new-checkout", "salt", "u_123", 25.0)  # -> bool
```

## Development

```bash
cd sdk/python
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest                                    # tests
.venv/bin/python -m pytest --cov=apdl --cov-report=term-missing  # tests + coverage
.venv/bin/ruff check apdl/ tests/                             # lint
```

CI runs the suite with `--cov-fail-under=88`; keep coverage at or above that
threshold.

The test suite pins golden hash values produced by the canonical config-service
implementation, guaranteeing this SDK buckets identically to the server and the
JS SDK.

## License

MIT
