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

client = APDL.init(api_key="proj_<project>_<secret>")  # secret: 16+ alphanumeric chars

# Track events (identity is explicit per call — servers handle many users)
client.track("order_completed", {"total": 42.0}, user_id="u_123")
client.identify("u_123", {"plan": "pro"})
client.group("org_42", {"name": "Acme"}, user_id="u_123")
client.page("/checkout", user_id="u_123")

# Evaluate a variant feature flag locally
if client.get_variant("new-checkout", user_id="u_123") == "treatment":
    ...

client.shutdown()  # flushes pending events and stops background threads
```

Or as a context manager (auto-shutdown):

```python
with APDL.init(api_key="proj_demo_0123456789abcdef") as client:
    client.track("signup", user_id="u_999")
```

## Configuration

Pass keyword args to `APDL.init(...)` or build an `APDLConfig`:

```python
from apdl import APDL, APDLConfig

client = APDL.init(APDLConfig(
    api_key="proj_demo_0123456789abcdef",
    host="https://ingest.apdl.dev",       # event ingestion endpoint
    config_host="https://config.apdl.dev",# flag config endpoint
    batch_size=20,                         # 1..100
    flush_interval=3.0,                    # seconds between background flushes
    max_queue_size=1000,                   # oldest events dropped past this
    enable_flags=True,                     # fetch + poll flag configs
    flag_poll_interval=30.0,               # seconds between flag refreshes
    log_exposures=True,                    # emit $feature_flag_exposure events
    request_timeout=10.0,
    debug=False,
))
```

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
