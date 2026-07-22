"""Execute compiled event selectors on the exact shipped ClickHouse engine."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest

from app.clickhouse.client import ClickHouseClient
from app.clickhouse.selectors import build_selector_condition
from app.models.schemas import EventSelector


def _selector(operator: str, value: Any = None) -> EventSelector:
    filter_: dict[str, Any] = {
        "property": "subject",
        "operator": operator,
    }
    if operator not in {"exists", "not_exists"}:
        filter_["value"] = value
    return EventSelector.model_validate(
        {
            "event_name": "selector_matrix",
            "filters": [filter_],
        }
    )


@asynccontextmanager
async def _exact_clickhouse_client() -> AsyncIterator[ClickHouseClient]:
    host = os.getenv("APDL_TEST_CLICKHOUSE_HOST")
    if not host:
        pytest.skip(
            "set APDL_TEST_CLICKHOUSE_HOST to execute selectors on pinned ClickHouse"
        )

    environment = {
        "CLICKHOUSE_HOST": host,
        "CLICKHOUSE_PORT": os.getenv("APDL_TEST_CLICKHOUSE_PORT", "9000"),
        "CLICKHOUSE_USER": os.getenv("APDL_TEST_CLICKHOUSE_USER", "apdl"),
        "CLICKHOUSE_PASSWORD": os.getenv(
            "APDL_TEST_CLICKHOUSE_PASSWORD",
            "apdl_dev",
        ),
        "CLICKHOUSE_DB": os.getenv("APDL_TEST_CLICKHOUSE_DB", "apdl"),
        "CLICKHOUSE_POOL_SIZE": "1",
    }
    with patch.dict(os.environ, environment):
        client = ClickHouseClient()
        await client.connect()
        try:
            yield client
        finally:
            await client.close()


async def _selector_matches(
    client: ClickHouseClient,
    selector: EventSelector,
    properties: dict[str, Any],
) -> bool:
    params: dict[str, Any] = {
        "row_event_name": selector.event_name,
        "row_properties": json.dumps(properties, separators=(",", ":")),
    }
    condition = build_selector_condition(selector, params, "exact")
    query = f"""
SELECT toUInt8({condition}) AS matched
FROM
(
    SELECT
        %(row_event_name)s AS event_name,
        %(row_properties)s AS properties
)
"""
    rows = await client.execute(query, params)
    assert len(rows) == 1
    return rows[0]["matched"] == 1


@pytest.mark.asyncio
async def test_every_accepted_selector_operator_executes_on_pinned_clickhouse():
    cases = [
        ("eq", "pro", {"subject": "pro"}),
        ("neq", "free", {"subject": "pro"}),
        ("in", ["pro", "team"], {"subject": "team"}),
        ("not_in", ["free", "starter"], {"subject": "team"}),
        ("exists", None, {"subject": "present"}),
        ("not_exists", None, {"different": "present"}),
        ("contains", "Start", {"subject": "Start checkout"}),
        ("gt", 4, {"subject": 5}),
        ("gte", 5, {"subject": 5}),
        ("lt", 6, {"subject": 5}),
        ("lte", 5, {"subject": 5}),
    ]

    async with _exact_clickhouse_client() as client:
        for operator, value, properties in cases:
            assert await _selector_matches(
                client,
                _selector(operator, value),
                properties,
            ), operator


@pytest.mark.asyncio
async def test_typed_selectors_reject_cross_type_values_on_pinned_clickhouse():
    cases = [
        ("eq", "5", {"subject": 5}),
        ("eq", 1, {"subject": "1"}),
        ("eq", True, {"subject": 1}),
        ("neq", "different", {"subject": 5}),
        ("neq", 2, {"subject": "1"}),
        ("neq", False, {"subject": 1}),
        ("in", ["5"], {"subject": 5}),
        ("in", [1], {"subject": "1"}),
        ("in", [True], {"subject": 1}),
        ("not_in", ["different"], {"subject": 5}),
        ("not_in", [2], {"subject": "1"}),
        ("not_in", [False], {"subject": 1}),
        ("contains", "5", {"subject": 5}),
        ("contains", "true", {"subject": True}),
        ("gt", 4, {"subject": "5"}),
        ("gt", 0, {"subject": True}),
        ("gte", 5, {"subject": "5"}),
        ("lt", 6, {"subject": "5"}),
        ("lte", 5, {"subject": "5"}),
    ]

    async with _exact_clickhouse_client() as client:
        for operator, value, properties in cases:
            assert not await _selector_matches(
                client,
                _selector(operator, value),
                properties,
            ), (operator, value, properties)
