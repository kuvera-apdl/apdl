#!/usr/bin/env python3
"""Print the canonical digest of the shipped Codegen egress policy sources."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_ROOT = ROOT / "infra/docker/codegen-egress"
POLICY_FILES = (
    (POLICY_ROOT / "Dockerfile", "codegen-egress/Dockerfile"),
    (POLICY_ROOT / "allowed-domains.txt", "codegen-egress/allowed-domains.txt"),
    (POLICY_ROOT / "entrypoint.sh", "codegen-egress/entrypoint.sh"),
    (POLICY_ROOT / "healthcheck.sh", "codegen-egress/healthcheck.sh"),
    (POLICY_ROOT / "policy.json", "codegen-egress/policy.json"),
    (POLICY_ROOT / "squid.conf", "codegen-egress/squid.conf"),
    (
        ROOT / "infra/docker/docker-compose.codegen-egress.yml",
        "docker-compose.codegen-egress.yml",
    ),
)


def main() -> None:
    files = []
    for path, relative in POLICY_FILES:
        payload = path.read_bytes()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {
        "schema_version": "codegen_egress_policy_source@1",
        "files": files,
    }
    canonical = json.dumps(
        manifest,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    print(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


if __name__ == "__main__":
    main()
