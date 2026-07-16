"""Real-Redis contract tests for hierarchical quota Lua semantics."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import redis.asyncio as redis

from app.middleware.rate_limit import BucketDebit, BucketLimit, _admit

TEST_REDIS_URL = os.environ.get("APDL_TEST_REDIS_URL")

pytestmark = pytest.mark.skipif(
    TEST_REDIS_URL is None,
    reason="APDL_TEST_REDIS_URL is required for real Redis contract tests",
)


@pytest.mark.asyncio
async def test_real_redis_child_rejection_preserves_parent_and_sibling() -> None:
    """A rejected child must leave every candidate balance unchanged."""
    client = redis.from_url(TEST_REDIS_URL, decode_responses=True)
    namespace = f"apdl:test:hierarchical-rate:{uuid4().hex}"
    keys = {
        "parent": f"{namespace}:parent",
        "sibling": f"{namespace}:sibling",
        "child": f"{namespace}:child",
    }
    accepted = [
        BucketDebit(keys["parent"], BucketLimit(10, 1), 1),
        BucketDebit(keys["sibling"], BucketLimit(10, 1), 1),
        BucketDebit(keys["child"], BucketLimit(1, 1), 1),
    ]
    rejected = [
        BucketDebit(keys["parent"], BucketLimit(10, 1), 1),
        BucketDebit(keys["sibling"], BucketLimit(10, 1), 1),
        # Cost exceeds capacity, so elapsed time cannot make this admissible.
        BucketDebit(keys["child"], BucketLimit(1, 1), 2),
    ]

    try:
        assert await _admit(client, accepted, quota_name="Test") is None
        balances_before = {
            name: await client.hgetall(key) for name, key in keys.items()
        }

        response = await _admit(client, rejected, quota_name="Test")

        assert response is not None
        assert response.status_code == 429
        assert {
            name: await client.hgetall(key) for name, key in keys.items()
        } == balances_before
    finally:
        await client.delete(*keys.values())
        await client.aclose()
