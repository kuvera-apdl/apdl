"""Validate a captured payload from the installed npm tarball."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.validation.schema import validate_event_batch


def main() -> None:
    payload = json.loads(Path(sys.argv[1]).read_text())
    result = validate_event_batch(payload)
    if not result["valid"]:
        raise SystemExit(f"packed SDK payload is invalid: {result['errors']}")

    events = payload["events"]
    expected = ["identify", "group", "page", "order_completed"]
    actual = [event["event"] for event in events]
    if actual != expected:
        raise SystemExit(f"unexpected canonical event names: {actual!r}")

    serialized = json.dumps(payload)
    for forbidden in (
        "anonymousId",
        "userId",
        "groupId",
        "messageId",
        "sessionId",
        "experiment_context",
        "device_type",
        "browser_version",
    ):
        if forbidden in serialized:
            raise SystemExit(f"packed SDK emitted forbidden field {forbidden!r}")


if __name__ == "__main__":
    main()
