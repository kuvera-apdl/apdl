"""End-to-end APDL Python SDK example against a local stack.

Prerequisites (see examples/README.md):
  1. `make dev-core` — supported core stack running on localhost
  2. The `new-checkout` flag created in the Admin Console
  3. `APDL_API_KEY` set to a confidential key created in Workspace settings
  4. `cd sdk/python && uv venv && uv pip install -e .`

Run:
  sdk/python/.venv/bin/python examples/python/track_and_gate.py
"""

import os

from apdl import APDL, APDLConfig

API_KEY = os.environ.get("APDL_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit(
        "APDL_API_KEY is required; create a confidential key in Workspace settings"
    )

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

    # --- Evaluate a flag locally (no network round-trip) ----------------
    result = client.get_variant_details("new-checkout", user_id="u_123")
    print(
        f"\nnew-checkout for u_123: variant={result.variant} "
        f"reason={result.reason} rollout_bucket={result.rollout_bucket}"
    )

    # Variant assignment is deterministic: the same user always buckets the
    # same way, in this SDK, the JS SDK, and the config service.
    assignments = {
        user_id: client.get_variant("new-checkout", user_id=user_id)
        for user_id in (f"u_{i}" for i in range(20))
    }
    print(f"variant assignment for 20 users: {assignments}")

# Exiting the context manager flushes pending events and stops background
# threads; events are now in Redis Streams on their way to ClickHouse.
print("\nDone — query the events via the Query Service (see examples/README.md).")
