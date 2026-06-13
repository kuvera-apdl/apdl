"""Shared fixtures and helpers for the SDK test suite."""

from __future__ import annotations

from typing import Any

import pytest

from apdl.flags.models import (
    FallthroughConfig,
    GateCondition,
    GateConfig,
    GateRule,
    RolloutConfig,
    VariantConfig,
)


def binary_variants() -> list[VariantConfig]:
    """A fresh control/treatment variant pair."""
    return [VariantConfig(key="control", weight=1), VariantConfig(key="treatment", weight=1)]


def make_flag(
    key: str = "flag",
    *,
    enabled: bool = True,
    default_variant: str = "control",
    variants: list[VariantConfig] | None = None,
    salt: str = "s",
    rules: list[GateRule] | None = None,
    fallthrough: FallthroughConfig | None = None,
    version: int = 1,
) -> GateConfig:
    return GateConfig(
        key=key,
        enabled=enabled,
        default_variant=default_variant,
        variants=variants if variants is not None else binary_variants(),
        salt=salt,
        rules=rules or [],
        fallthrough=fallthrough
        or FallthroughConfig(rollout=RolloutConfig(percentage=100.0, bucket_by="user_id")),
        version=version,
    )


def make_rule(
    conditions: list[GateCondition],
    *,
    rule_id: str = "r1",
    percentage: float = 100.0,
    bucket_by: str = "user_id",
) -> GateRule:
    return GateRule(
        id=rule_id,
        conditions=conditions,
        rollout=RolloutConfig(percentage=percentage, bucket_by=bucket_by),
    )


class RecordingTransport:
    """Fake transport capturing posts and returning a scriptable result."""

    def __init__(self, *, ok: bool = True, flags: Any = None) -> None:
        self.ok = ok
        self.flags = flags
        self.posts: list[tuple[str, Any]] = []
        self.closed = False

    def post_json(self, url: str, payload: Any) -> bool:
        self.posts.append((url, payload))
        return self.ok

    def get_json(self, url: str) -> Any:
        return self.flags

    @property
    def headers(self) -> dict[str, str]:
        return {}

    def close(self) -> None:
        self.closed = True

    # Convenience: every event posted across all batches, flattened.
    def all_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for _url, payload in self.posts:
            events.extend(payload.get("events", []))
        return events


@pytest.fixture
def recording_transport() -> RecordingTransport:
    return RecordingTransport()
