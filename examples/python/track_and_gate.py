"""End-to-end APDL Python SDK example against a local stack.

Prerequisites (see examples/README.md):
  1. `make dev-all` — stack running on localhost
  2. The `new-checkout` flag created via the config admin API
  3. `cd sdk/python && uv venv && uv pip install -e .`

Run:
  sdk/python/.venv/bin/python examples/python/track_and_gate.py
"""

from apdl import APDL, APDLConfig

API_KEY = "proj_demo_0123456789abcdef"

config = APDLConfig(
    api_key=API_KEY,
    endpoint="http://localhost:8000",
    flush_interval=1.0,
    debug=True,
)

with APDL.init(config) as client:
    # --- Track events (identity is explicit per call) -------------------
    client.identify("u_123", {"plan": "pro"})
    client.page("/checkout", user_id="u_123")
    client.track("order_completed", {"total": 42.0, "items": 3}, user_id="u_123")

    # --- Evaluate a gate locally (no network round-trip) ----------------
    result = client.check_gate_details("new-checkout", user_id="u_123")
    print(
        f"\nnew-checkout for u_123: value={result.value} "
        f"reason={result.reason} bucket={result.bucket}"
    )

    # The 50% rollout is deterministic: the same user always buckets the
    # same way, in this SDK, the JS SDK, and the config service.
    enabled = [u for u in (f"u_{i}" for i in range(20)) if client.check_gate("new-checkout", user_id=u)]
    print(f"users in rollout: {len(enabled)}/20 -> {enabled}")

# Exiting the context manager flushes pending events and stops background
# threads; events are now in Redis Streams on their way to ClickHouse.
print("\nDone — query the events via the Query Service (see examples/README.md).")
