"""Contracts for the fenced, auditable analytics deletion workflow."""

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "pipeline" / "clickhouse"
MODULE_PATH = MODULE_DIR / "delete_analytics_data.py"
POSTGRES_SQL = (
    ROOT
    / "pipeline"
    / "postgres"
    / "migrations"
    / "040_analytics_data_deletion_audit.sql"
).read_text()
sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location("apdl_analytics_deletion", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
deletion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = deletion
SPEC.loader.exec_module(deletion)


def _request_args(**overrides: str) -> Namespace:
    values = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "scope": "user",
        "project_id": "demo",
        "user_id": "user@example.test",
        "actor": "privacy@example.test",
        "reason": "verified erasure request",
    }
    values.update(overrides)
    return Namespace(**values)


def _completed_event(request) -> dict[str, object]:
    return {
        "event_type": "completed",
        "scope": request.scope,
        "project_id": request.project_id,
        "target_sha256": request.target_sha256,
        "request_sha256": request.request_sha256,
        "actor": request.actor,
        "reason": request.reason,
        "details": {
            "anonymous_id_count": 1,
            "matched_rows": {table: 0 for table in deletion.TARGET_TABLES},
        },
    }


def test_deletion_request_has_one_strict_hashed_contract():
    request = deletion._request_from_args(_request_args())

    assert request.request_id == "11111111-1111-4111-8111-111111111111"
    assert request.scope == "user"
    assert len(request.target_sha256) == 64
    assert len(request.request_sha256) == 64
    assert request.user_id not in request.target_sha256
    assert request.target_sha256 != request.request_sha256

    with pytest.raises(deletion.DeletionError, match="canonical lowercase UUID"):
        deletion._request_from_args(
            _request_args(request_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")
        )
    with pytest.raises(deletion.DeletionError, match="project_id"):
        deletion._request_from_args(_request_args(project_id="demo-project"))
    with pytest.raises(deletion.DeletionError, match="user_id"):
        deletion._request_from_args(_request_args(user_id="x" * 129))


def test_deletion_targets_are_explicit_and_keep_alias_assertions_until_last():
    assert deletion.TARGET_TABLES == (
        "feature_flag_exposures",
        "frontend_health_events",
        "sessions",
        "experiment_event_deliveries",
        "events",
        "identity_alias_assertions",
    )
    assert deletion.REQUIRED_POSTGRES_MIGRATION == (
        40,
        "040_analytics_data_deletion_audit.sql",
    )
    assert deletion.REQUIRED_CLICKHOUSE_MIGRATION == (
        16,
        "016_personal_data_retention.sql",
    )


def test_user_condition_is_tenant_bound_and_does_not_embed_raw_identifiers():
    request = deletion._request_from_args(_request_args())

    condition = deletion._target_condition(request, ("YW5vbi0x", "YW5vbi0y"))

    assert "project_id =" in condition
    assert "user_id =" in condition
    assert "anonymous_id IN" in condition
    assert "user@example.test" not in condition
    assert "demo" not in condition
    assert "base64Decode(" in condition


def test_completed_request_is_idempotent_without_repeating_mutations(monkeypatch):
    request = deletion._request_from_args(_request_args())
    completion = _completed_event(request)

    monkeypatch.setattr(
        deletion,
        "_read_audit_events",
        lambda _request_id, _fence: {"completed": completion},
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("completed request repeated analytics mutations")

    monkeypatch.setattr(deletion, "_linked_anonymous_ids", unexpected)
    monkeypatch.setattr(deletion, "_delete_target_rows", unexpected)

    result = deletion.execute_request(request, object(), object())

    assert result["status"] == "already_completed"
    assert result["details"] == completion["details"]
    assert "user_id" not in result


def test_audit_ledger_is_append_only_and_never_stores_raw_user_ids():
    table_definition = POSTGRES_SQL.split(
        "CREATE TABLE analytics_data_deletion_audit (", 1
    )[1].split("\n);", 1)[0]

    assert "user_id" not in table_definition
    assert "target_sha256  TEXT NOT NULL" in table_definition
    assert "request_sha256 TEXT NOT NULL" in table_definition
    assert "PRIMARY KEY (request_id, event_type)" in table_definition
    assert "event_type IN ('requested', 'completed')" in table_definition
    assert "scope IN ('project', 'user')" in table_definition
    assert "details = '{}'::jsonb" in table_definition
    assert "details ?& ARRAY[" in table_definition
    for table in deletion.TARGET_TABLES:
        assert f"'{table}'" in table_definition
        assert f"{{matched_rows,{table}}}" in table_definition
    assert "NEW.recorded_at := clock_timestamp()" in POSTGRES_SQL
    assert POSTGRES_SQL.count("SET search_path = pg_catalog, public") == 2
    assert "analytics deletion completion requires a requested event" in POSTGRES_SQL
    assert "analytics deletion completion does not match its request" in POSTGRES_SQL
    assert "BEFORE UPDATE OR DELETE" in POSTGRES_SQL
    assert "BEFORE TRUNCATE" in POSTGRES_SQL
    assert "REVOKE ALL ON analytics_data_deletion_audit FROM PUBLIC" in POSTGRES_SQL
