"""Atomic storage contracts for Agents activation and deactivation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.store.llm_setup import (
    AgentsSetupAuthorizationError,
    AgentsSetupConflictError,
    AgentsSetupStore,
    AgentsSetupValidationError,
    ModelSelection,
)


ACTOR_ID = UUID("20000000-0000-4000-8000-000000000002")
OTHER_ID = UUID("20000000-0000-4000-8000-000000000003")
MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "pipeline"
    / "postgres"
    / "migrations"
    / "051_agents_project_setup.sql"
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Acquire:
    def __init__(self, conn: "_SetupConn") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_SetupConn":
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: "_SetupConn") -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class _SetupConn:
    def __init__(
        self,
        *,
        owner_user_id: UUID | None = ACTOR_ID,
        account_active: bool = True,
        roles: list[str] | None = None,
        policy_state: str = "inactive",
        policy_version: int = 0,
    ) -> None:
        self.owner_user_id = owner_user_id
        self.account_active = account_active
        self.roles = roles
        self.selection_rows: list[dict[str, Any] | None] = [
            {
                "supported_tiers": ["fast", "reasoning"],
                "model_catalog_version": "llm-provider-catalog@2",
                "connection_catalog_version": "llm-provider-catalog@2",
                "credential_state": "active",
            },
            {
                "supported_tiers": ["fast", "reasoning"],
                "model_catalog_version": "llm-provider-catalog@2",
                "connection_catalog_version": "llm-provider-catalog@2",
                "credential_state": "active",
            },
        ]
        self.assignment_rows: list[dict[str, Any]] = []
        self.connection_rows: list[dict[str, Any]] = []
        self.model_rows: list[dict[str, Any]] = []
        self.policy = {
            "project_id": "demo",
            "state": policy_state,
            "version": policy_version,
            "required_data_residency": "global",
            "allow_cross_vendor_retry": False,
            "project_daily_cost_limit_usd_micros": 20_000_000,
            "run_cost_limit_usd_micros": 2_000_000,
            "activated_by_actor_user_id": (
                ACTOR_ID if policy_version > 0 else None
            ),
            "activated_at": None,
            "deactivated_by_actor_user_id": None,
            "deactivation_reason": None,
            "deactivated_at": None,
            "effectful_execution_authorization_source": None,
        }
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_queries: list[str] = []

    def transaction(self, **kwargs: object) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.fetchrow_queries.append(query)
        if "SELECT project.owner_user_id, account.active" in query:
            return {
                "owner_user_id": self.owner_user_id,
                "active": self.account_active,
            }
        if "SELECT policy.*" in query:
            return self.policy
        if "SELECT model.supported_tiers" in query:
            return self.selection_rows.pop(0)
        if "SELECT membership.roles AS previous_roles" in query:
            return {
                "previous_roles": [
                    "agents:read",
                    "credentials:manage",
                    "members:manage",
                ],
                "email": "owner@example.com",
                "next_roles": [
                    "agents:read",
                    "agents:run",
                    "agents:manage",
                    "credentials:manage",
                    "members:manage",
                ],
            }
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "SELECT roles" in query:
            return self.roles
        raise AssertionError(query)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM llm_project_model_assignments AS assignment" in query:
            return self.assignment_rows
        if "FROM llm_project_provider_connections AS connection" in query:
            return self.connection_rows
        if "FROM llm_project_provider_models AS model" in query:
            return self.model_rows
        raise AssertionError(query)

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"


def _selection() -> ModelSelection:
    return ModelSelection(
        provider="openai",
        model="gpt-5.4-mini",
        connection_version=3,
        inventory_version=2,
    )


@pytest.mark.asyncio
async def test_first_owner_activation_adds_analysis_roles_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _SetupConn()
    store = AgentsSetupStore(_Pool(conn))
    returned = object()

    async def fake_get(project_id: str, *, actor_user_id: UUID | None) -> object:
        assert (project_id, actor_user_id) == ("demo", ACTOR_ID)
        return returned

    monkeypatch.setattr(store, "get", fake_get)
    result = await store.put(
        "demo",
        fast_model=_selection(),
        reasoning_model=_selection(),
        expected_version=0,
        actor_user_id=ACTOR_ID,
    )

    assert result is returned
    role_update = next(
        args
        for query, args in conn.executed
        if "UPDATE admin_user_projects" in query
    )
    assert set(role_update[2]) >= {"agents:run", "agents:manage"}
    assert "agents:approve" not in role_update[2]
    assert not any(
        "admin_project_execution_authorizations" in query
        for query, _ in conn.executed
    )
    assignment_inserts = [
        args
        for query, args in conn.executed
        if "INSERT INTO llm_project_model_assignments" in query
    ]
    assert [args[1] for args in assignment_inserts] == ["fast", "reasoning"]
    setup_audit = next(
        args
        for query, args in conn.executed
        if "INSERT INTO llm_project_setup_audit" in query
    )
    assert setup_audit[1] == "activate"
    assert setup_audit[3] == 1
    assert '"connection_version": 3' in setup_audit[5]
    assert '"inventory_version": 2' in setup_audit[5]
    assert any(
        args == ("apdl:llm-vault:demo:openai",)
        for query, args in conn.executed
        if "pg_advisory_xact_lock" in query
    )


@pytest.mark.asyncio
async def test_selection_locks_projections_without_vault_update_privilege() -> None:
    conn = _SetupConn()

    await AgentsSetupStore._validate_selection(
        conn,
        project_id="demo",
        tier="fast",
        selection=_selection(),
    )

    query = next(
        query
        for query in conn.fetchrow_queries
        if "SELECT model.supported_tiers" in query
    )
    assert "FOR SHARE OF connection, model" in query
    assert "credential, consumer" not in query


@pytest.mark.asyncio
async def test_activation_validates_both_selections_before_mutation() -> None:
    conn = _SetupConn()
    conn.selection_rows[1] = None
    store = AgentsSetupStore(_Pool(conn))

    with pytest.raises(
        AgentsSetupConflictError,
        match="reasoning model selection is stale",
    ):
        await store.put(
            "demo",
            fast_model=_selection(),
            reasoning_model=_selection(),
            expected_version=0,
            actor_user_id=ACTOR_ID,
        )

    assert not any(
        "DELETE FROM llm_project_model_assignments" in query
        or "DELETE FROM llm_project_provider_policies" in query
        or "UPDATE llm_project_policies" in query
        for query, _ in conn.executed
    )


@pytest.mark.asyncio
async def test_activation_supports_cross_provider_assignments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _SetupConn()
    store = AgentsSetupStore(_Pool(conn))

    async def fake_get(project_id: str, *, actor_user_id: UUID | None) -> object:
        return object()

    monkeypatch.setattr(store, "get", fake_get)
    await store.put(
        "demo",
        fast_model=_selection(),
        reasoning_model=ModelSelection(
            provider="anthropic",
            model="claude-sonnet-4-6",
            connection_version=8,
            inventory_version=5,
        ),
        expected_version=0,
        actor_user_id=ACTOR_ID,
    )

    policy_inserts = [
        args
        for query, args in conn.executed
        if "INSERT INTO llm_project_provider_policies" in query
    ]
    assert [(args[1], args[2]) for args in policy_inserts] == [
        ("openai", "gpt-5.4-mini"),
        ("anthropic", "claude-sonnet-4-6"),
    ]


@pytest.mark.asyncio
async def test_activation_rejects_model_that_is_ineligible_for_tier() -> None:
    conn = _SetupConn()
    conn.selection_rows[1]["supported_tiers"] = ["fast"]
    store = AgentsSetupStore(_Pool(conn))

    with pytest.raises(
        AgentsSetupValidationError,
        match="not eligible for the reasoning tier",
    ):
        await store.put(
            "demo",
            fast_model=_selection(),
            reasoning_model=_selection(),
            expected_version=0,
            actor_user_id=ACTOR_ID,
        )

    assert not any(
        "DELETE FROM llm_project_model_assignments" in query
        for query, _ in conn.executed
    )


@pytest.mark.asyncio
async def test_live_delegated_authority_requires_both_roles() -> None:
    conn = _SetupConn(
        owner_user_id=OTHER_ID,
        roles=["agents:manage"],
    )
    store = AgentsSetupStore(_Pool(conn))

    with pytest.raises(AgentsSetupAuthorizationError):
        await store.put(
            "demo",
            fast_model=_selection(),
            reasoning_model=_selection(),
            expected_version=0,
            actor_user_id=ACTOR_ID,
        )

    conn.roles = ["agents:manage", "credentials:manage"]
    authority = await store._management_authority(
        conn,
        "demo",
        ACTOR_ID,
        lock=True,
    )
    assert authority == "delegated"


@pytest.mark.asyncio
async def test_deactivation_preserves_assignments_and_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _SetupConn(policy_state="active", policy_version=4)
    conn.assignment_rows = [
        {
            "tier": tier,
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "model_catalog_version": "llm-provider-catalog@2",
            "connection_version": 3,
            "inventory_version": 2,
        }
        for tier in ("fast", "reasoning")
    ]
    store = AgentsSetupStore(_Pool(conn))
    returned = object()

    async def fake_get(project_id: str, *, actor_user_id: UUID | None) -> object:
        assert (project_id, actor_user_id) == ("demo", ACTOR_ID)
        return returned

    monkeypatch.setattr(store, "get", fake_get)
    result = await store.deactivate(
        "demo",
        expected_version=4,
        actor_user_id=ACTOR_ID,
        reason="Paused by owner",
    )

    assert result is returned
    statements = "\n".join(query for query, _ in conn.executed)
    assert "DELETE FROM llm_project_model_assignments" not in statements
    assert "DELETE FROM llm_project_provider_connections" not in statements
    policy_update = next(
        args
        for query, args in conn.executed
        if "UPDATE llm_project_policies" in query
    )
    assert policy_update == ("demo", 5, ACTOR_ID, "Paused by owner")
    assert "UPDATE agent_runs AS run" in statements
    assert "UPDATE custom_agent_test_runs" in statements
    assert "UPDATE agent_approval_effects AS effect" in statements


@pytest.mark.asyncio
async def test_get_preserves_catalog_stale_assignment_as_noncurrent() -> None:
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    conn = _SetupConn(policy_state="active", policy_version=4)
    conn.policy["activated_at"] = now
    conn.assignment_rows = [
        {
            "tier": "fast",
            "provider": "openai",
            "model": "retired-model",
            "model_catalog_version": "llm-provider-catalog@1",
            "assigned_at": now,
            "updated_at": now,
            "connection_version": 3,
            "inventory_version": 2,
            "policy_endpoint_url": "https://api.openai.com/v1",
            "policy_data_residency": "global",
            "policy_allowed_data_classifications": ["public", "internal"],
            "policy_input_cost": 100,
            "policy_output_cost": 200,
        }
    ]
    conn.connection_rows = [
        {
            "provider": "openai",
            "version": 3,
            "inventory_version": 2,
            "state": "active",
            "catalog_version": "llm-provider-catalog@1",
            "credential_active": True,
            "validated_at": now,
        }
    ]
    conn.model_rows = [
        {
            "provider": "openai",
            "model_id": "retired-model",
            "display_name": "Retired model",
            "connection_version": 3,
            "inventory_version": 2,
            "catalog_version": "llm-provider-catalog@1",
            "supported_tiers": ["fast"],
            "data_residency": "global",
            "allowed_data_classifications": ["public", "internal"],
        }
    ]

    setup = await AgentsSetupStore(_Pool(conn)).get(
        "demo",
        actor_user_id=ACTOR_ID,
    )

    assert len(setup.assignments) == 1
    assignment = setup.assignments[0]
    assert assignment.model == "retired-model"
    assert assignment.model_catalog_version == "llm-provider-catalog@1"
    assert assignment.current is False
    assert "catalog_stale" in setup.blockers
    assert "reasoning_model_required" in setup.blockers
    assert setup.analysis_ready is False


def test_migration_splits_analysis_activation_from_operator_effect_authority() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN state TEXT NOT NULL DEFAULT 'inactive'" in sql
    assert "SET DEFAULT 20000000" in sql
    assert "SET DEFAULT 2000000" in sql
    assert "DELETE FROM llm_project_model_assignments;" in sql
    assert "VALUES (NEW.project_id)" in sql
    assert "'local'" not in sql
    assert "'agents:approve' = ANY(NEW.roles)" in sql
    assert "'agents:run' = ANY(NEW.roles)" not in sql
    assert "'agents:manage' = ANY(NEW.roles)" not in sql
    for table in (
        "public.agent_runs",
        "public.custom_agent_test_runs",
        "public.llm_calls",
        "public.llm_provider_attempts",
    ):
        assert f"'{table}'::regclass" in sql
    assert "admin_project_execution_authorizations" not in sql[
        sql.index("CREATE OR REPLACE FUNCTION apdl_assert_agents_project_active"):
        sql.index("CREATE OR REPLACE FUNCTION apdl_enforce_analysis_table_project")
    ]
    assert "llm_project_setup_audit_no_update_delete" in sql
    assert "llm_project_setup_audit_no_truncate" in sql
