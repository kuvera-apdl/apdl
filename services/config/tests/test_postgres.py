import json

import pytest

from app import main
from app.store import postgres


@pytest.mark.asyncio
async def test_get_flags_filters_client_visible_modes():
    pool = RecordingPool()

    await postgres.get_flags(pool, "apdl", client_visible_only=True)

    assert "evaluation_mode IN ('client', 'both')" in pool.sql
    assert "client_exposed" not in pool.sql
    assert "SELECT *" not in pool.sql
    assert "default_variant" in pool.sql
    assert "variants" in pool.sql
    assert "default_value" not in pool.sql
    assert pool.args == ("apdl",)


@pytest.mark.asyncio
async def test_get_flag_uses_explicit_canonical_projection():
    pool = RecordingFetchRowPool(row=None)

    await postgres.get_flag(pool, "apdl", "checkout")

    assert "SELECT *" not in pool.sql
    assert "default_variant" in pool.sql
    assert "variants" in pool.sql
    assert "default_value" not in pool.sql
    assert pool.args == ("apdl", "checkout")


@pytest.mark.asyncio
async def test_create_flag_writes_canonical_defaults_when_internal_payload_omits_them():
    pool = RecordingFetchRowPool(row=make_row())

    created = await postgres.create_flag(pool, {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "salt": "salt_123",
    })

    assert created is not None
    assert "RETURNING *" not in pool.sql
    assert "default_value" not in pool.sql
    assert json.loads(pool.args[9]) == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]
    assert json.loads(pool.args[11]) == {
        "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
    }


@pytest.mark.asyncio
async def test_update_flag_uses_explicit_canonical_returning():
    pool = RecordingFetchRowPool(row=make_row())

    updated = await postgres.update_flag(pool, make_row(), expected_version=1)

    assert updated is not None
    assert "RETURNING *" not in pool.sql
    assert "default_variant" in pool.sql
    assert "variants" in pool.sql
    assert "default_value" not in pool.sql


@pytest.mark.asyncio
async def test_archive_flag_uses_explicit_canonical_returning():
    pool = RecordingFetchRowPool(row=make_row({"state": "archived"}))

    archived = await postgres.archive_flag(pool, "apdl", "checkout")

    assert archived is not None
    assert "RETURNING *" not in pool.sql
    assert "default_variant" in pool.sql
    assert "variants" in pool.sql
    assert "default_value" not in pool.sql


@pytest.mark.asyncio
async def test_disable_flag_uses_explicit_canonical_returning():
    pool = RecordingFetchRowPool(row=make_row({"state": "disabled"}))

    disabled = await postgres.disable_flag(
        pool,
        project_id="apdl",
        key="checkout",
        reason="guardrail_failed",
        source="system",
    )

    assert disabled is not None
    assert "RETURNING *" not in pool.sql
    assert "default_variant" in pool.sql
    assert "variants" in pool.sql
    assert "default_value" not in pool.sql


def test_row_to_flag_omits_obsolete_columns_from_legacy_records():
    row = {
        **make_row(),
        "default_value": False,
        "variant_type": "boolean",
        "variants_json": "[]",
        "rollout_percentage": 100,
        "client_exposed": True,
    }

    flag = postgres._row_to_flag(row)

    assert "default_value" not in flag
    assert "variant_type" not in flag
    assert "variants_json" not in flag
    assert "rollout_percentage" not in flag
    assert "client_exposed" not in flag
    assert flag["default_variant"] == "control"
    assert flag["variants"] == [
        {"key": "control", "weight": 1},
        {"key": "treatment", "weight": 1},
    ]


def test_create_flags_table_defines_only_canonical_variant_columns():
    create_sql = main.CREATE_FLAGS_TABLE

    assert "default_variant TEXT NOT NULL DEFAULT 'control'" in create_sql
    assert "variants JSONB NOT NULL DEFAULT" in create_sql
    assert "default_value" not in create_sql
    assert "variant_type" not in create_sql
    assert "variants_json" not in create_sql
    assert "fallthrough JSONB NOT NULL DEFAULT" in create_sql
    assert '"value"' not in create_sql
    assert "flags_fallthrough_rollout_only_check" in create_sql


def test_migration_rewrites_and_drops_legacy_boolean_flag_shape():
    migrate_sql = main.MIGRATE_FLAGS_TABLE

    assert "ADD COLUMN IF NOT EXISTS default_variant" in migrate_sql
    assert "ADD COLUMN IF NOT EXISTS variants" in migrate_sql
    assert "ADD COLUMN IF NOT EXISTS default_value" not in migrate_sql
    assert "DROP COLUMN IF EXISTS default_value" in migrate_sql
    assert "DROP COLUMN IF EXISTS variant_type" in migrate_sql
    assert "DROP COLUMN IF EXISTS variants_json" in migrate_sql
    assert "DROP COLUMN IF EXISTS rollout_percentage" in migrate_sql
    assert "DROP COLUMN IF EXISTS client_exposed" in migrate_sql
    assert "DROP TABLE feature_flags" in migrate_sql
    assert "fallthrough - 'rollout'" in migrate_sql
    assert "ALTER COLUMN default_variant SET NOT NULL" in migrate_sql
    assert "flags_fallthrough_rollout_only_check" in migrate_sql


class RecordingPool:
    sql: str = ""
    args: tuple = ()

    async def fetch(self, sql: str, *args):
        self.sql = sql
        self.args = args
        return []


class RecordingFetchRowPool:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.args = ()

    async def fetchrow(self, sql: str, *args):
        self.sql = sql
        self.args = args
        return self.row


def make_row(overrides: dict | None = None) -> dict:
    row = {
        "key": "checkout",
        "project_id": "apdl",
        "name": "Checkout",
        "state": "draft",
        "owners": [],
        "review_by": None,
        "enabled": False,
        "description": "",
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 1},
        ],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 0.0, "bucket_by": "user_id"},
        },
        "salt": "salt_123",
        "evaluation_mode": "client",
        "auto_disable": True,
        "guardrails": [],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "version": 1,
        "created_at": "2026-06-01T00:00:00+00:00",
        "updated_at": "2026-06-01T00:00:00+00:00",
        "archived_at": None,
    }
    if overrides:
        row.update(overrides)
    return row
