"""Strict approval API and durable command/outbox regression tests."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import httpx
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.auth import Principal, authenticate_request
from app.main import app
from app.routers import approvals
from app.store import approval_effects
from app.store.approval_effects import (
    ApprovalCapabilityError,
    ApprovalCommandView,
    ApprovalDecision,
    ApprovalDecisionError,
    ApprovalEffectView,
    ApprovalGateConflictError,
    PermanentApprovalEffectError,
    RetryableApprovalEffectError,
    _ClaimedEffect,
    _classify_effect_error,
    _execute_effect,
    enqueue_approval_command,
    process_one_approval_effect,
)


def _view(status: str = "queued") -> ApprovalCommandView:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    return ApprovalCommandView(
        command_id="11111111-1111-4111-8111-111111111111",
        run_id="run-1",
        actor_credential_id="test-agents",
        actor_user_id="20000000-0000-4000-8000-000000000002",
        gate_id="run-1:code_implementation",
        gate_agent="code_implementation",
        status=status,
        approved_count=1,
        rejected_count=0,
        comment="ship it",
        last_error=None,
        created_at=now,
        updated_at=now,
        effects=(
            ApprovalEffectView(
                effect_id="22222222-2222-4222-8222-222222222222",
                item_id="p1",
                effect_type="open_code_changeset",
                status="queued",
                attempt_count=0,
                last_error=None,
                result=None,
            ),
        ),
    )


def _client() -> AsyncClient:
    app.state.pg_pool = object()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def codegen_available(monkeypatch):
    async def available() -> str:
        return "available"

    monkeypatch.setattr(approvals, "codegen_changeset_capability", available)


@pytest.mark.asyncio
async def test_post_returns_only_the_queued_command_envelope(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_enqueue(pool: Any, **kwargs: Any) -> ApprovalCommandView:
        captured.update(kwargs)
        return _view()

    monkeypatch.setattr(approvals, "enqueue_approval_command", fake_enqueue)

    async with _client() as client:
        response = await client.post(
            "/v1/agents/run-1/approve",
            json={
                "decisions": [{"item_id": "p1", "approved": True}],
                "comment": "ship it",
            },
        )

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "command_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "run-1",
        "actor_credential_id": "test-agents",
        "actor_user_id": "20000000-0000-4000-8000-000000000002",
        "gate_id": "run-1:code_implementation",
        "gate_agent": "code_implementation",
        "status": "queued",
        "approved_count": 1,
        "rejected_count": 0,
        "comment": "ship it",
        "last_error": None,
        "created_at": "2026-07-16T00:00:00Z",
        "updated_at": "2026-07-16T00:00:00Z",
        "effects": [
            {
                "effect_id": "22222222-2222-4222-8222-222222222222",
                "item_id": "p1",
                "effect_type": "open_code_changeset",
                "status": "queued",
                "attempt_count": 0,
                "last_error": None,
                "result": None,
            }
        ],
    }
    assert "opened_changesets" not in body
    assert "forked_runs" not in body
    assert "errors" not in body
    assert captured["decisions"] == (ApprovalDecision("p1", True),)
    assert captured["actor_credential_id"] == "test-agents"
    assert captured["actor_user_id"] is None
    assert captured["codegen_changeset_capability"] == "available"


@pytest.mark.asyncio
async def test_post_rejects_codegen_effect_when_capability_is_disabled(
    monkeypatch,
) -> None:
    async def disabled() -> str:
        return "disabled"

    async def fake_enqueue(pool: Any, **kwargs: Any) -> ApprovalCommandView:
        del pool
        raise ApprovalCapabilityError(kwargs["codegen_changeset_capability"])

    monkeypatch.setattr(approvals, "codegen_changeset_capability", disabled)
    monkeypatch.setattr(approvals, "enqueue_approval_command", fake_enqueue)

    async with _client() as client:
        response = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "p1", "approved": True}]},
        )

    assert response.status_code == 424
    assert "changeset creation is disabled" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"approved": True},
        {"decisions": []},
        {
            "decisions": [
                {"item_id": "p1", "approved": True},
                {"item_id": "p1", "approved": False},
            ]
        },
        {"decisions": [{"item_id": " p1", "approved": True}]},
        {"decisions": [{"item_id": "p1", "approved": "true"}]},
        {"decisions": [{"item_id": "p1", "approved": 1}]},
        {"decisions": [{"item_id": "p1", "approved": True, "extra": 1}]},
        {"decisions": [{"item_id": "p1", "approved": True}], "extra": 1},
    ],
)
async def test_request_rejects_legacy_ambiguous_or_extra_shapes(payload) -> None:
    async with _client() as client:
        response = await client.post("/v1/agents/run-1/approve", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_request_bounds_decisions_and_comment() -> None:
    too_many = [{"item_id": f"p{index}", "approved": True} for index in range(101)]
    async with _client() as client:
        decisions = await client.post(
            "/v1/agents/run-1/approve", json={"decisions": too_many}
        )
        comment = await client.post(
            "/v1/agents/run-1/approve",
            json={
                "decisions": [{"item_id": "p1", "approved": True}],
                "comment": "x" * 2001,
            },
        )
    assert decisions.status_code == 422
    assert comment.status_code == 422


@pytest.mark.asyncio
async def test_approval_requires_agents_approve_role() -> None:
    async def authenticate_runner(request: Request) -> Principal:
        principal = Principal(
            credential_id="runner",
            project_id="demo",
            roles=frozenset({"agents:run"}),
            self_registered_project=False,
            execution_authorized=True,
        )
        request.state.principal = principal
        return principal

    app.dependency_overrides[authenticate_request] = authenticate_runner
    async with _client() as client:
        response = await client.post(
            "/v1/agents/run-1/approve",
            json={"decisions": [{"item_id": "p1", "approved": True}]},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_status_endpoint_exposes_effect_retry_state(monkeypatch) -> None:
    async def fake_get(*args: Any, **kwargs: Any) -> ApprovalCommandView:
        return _view(status="processing")

    monkeypatch.setattr(approvals, "get_approval_command", fake_get)
    async with _client() as client:
        response = await client.get(
            "/v1/agents/run-1/approvals/11111111-1111-4111-8111-111111111111"
        )
    assert response.status_code == 200
    assert response.json()["status"] == "processing"
    assert response.json()["effects"][0]["status"] == "queued"


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _CommandConn:
    def __init__(self, *, fail_audit: bool = False) -> None:
        self.fail_audit = fail_audit
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.command_id: uuid.UUID | None = None
        self.run_status = "waiting_approval"
        self.persisted_command: dict[str, Any] | None = None
        self.effect_rows: list[dict[str, Any]] = []
        self.gate_item: dict[str, Any] = {
            "proposal_id": "p1",
            "title": "Ship durable effects",
            "spec": "Implement the approved change.",
            "decision": "approve",
            "safety_result": {
                "passed": True,
                "checks": [],
                "risk_level": "low",
            },
        }
        self.now = datetime(2026, 7, 16, tzinfo=UTC)

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.statements.append((query, args))
        if "FROM agent_runs" in query:
            return {
                "run_id": "run-1",
                "project_id": "demo",
                "status": self.run_status,
                "phase": "code_implementation_approval",
            }
        if "FROM agent_approval_commands" in query:
            if self.persisted_command is None:
                return None
            return (
                self.persisted_command
                if self.persisted_command["request_sha256"] == args[2]
                else None
            )
        if "FROM agent_run_results" in query:
            return {
                "agent_name": "code_implementation",
                "produces": "changesets",
                "output": json.dumps([self.gate_item]),
                "metadata": json.dumps(
                    {
                        "needs_approval": True,
                        "approval_gate": {
                            "gate_id": "run-1:code_implementation",
                            "agent_name": "code_implementation",
                            "produces": "changesets",
                            "phase": "code_implementation_approval",
                            "state": "pending",
                        },
                    }
                ),
            }
        if "INSERT INTO agent_approval_commands" in query:
            self.command_id = args[0]
            self.persisted_command = {
                "command_id": args[0],
                "run_id": "run-1",
                "actor_credential_id": args[3],
                "actor_user_id": args[11],
                "request_sha256": args[4],
                "gate_id": args[5],
                "gate_agent": args[6],
                "status": "queued",
                "approved_count": args[8],
                "rejected_count": args[9],
                "comment": args[10],
                "last_error": None,
                "created_at": self.now,
                "updated_at": self.now,
            }
            return self.persisted_command
        if "INSERT INTO agent_approval_effects" in query:
            row = {
                "effect_id": args[0],
                "item_id": args[4],
                "effect_type": args[5],
                "status": "queued",
                "attempt_count": 0,
                "last_error": None,
                "result": None,
            }
            self.effect_rows.append(row)
            return row
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append((query, args))
        if "FROM agent_approval_effects" in query:
            return list(self.effect_rows)
        raise AssertionError(f"Unexpected fetch query: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append((query, args))
        if self.fail_audit and "INSERT INTO agent_audit_log" in query:
            raise RuntimeError("audit unavailable")
        if "SET status = 'approval_queued'" in query:
            self.run_status = "approval_queued"
        return "OK"


class _Acquire:
    def __init__(self, conn: _CommandConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _CommandConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _CommandConn) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


@pytest.mark.asyncio
async def test_enqueue_persists_decision_audit_and_effect_before_queue_transition() -> (
    None
):
    conn = _CommandConn()
    command = await enqueue_approval_command(
        _Pool(conn),
        run_id="run-1",
        project_id="demo",
        actor_credential_id="test-agents",
        actor_user_id="20000000-0000-4000-8000-000000000002",
        codegen_changeset_capability="available",
        decisions=(ApprovalDecision("p1", True),),
        comment="ship it",
    )

    assert command.status == "queued"
    assert command.actor_credential_id == "test-agents"
    assert command.actor_user_id == "20000000-0000-4000-8000-000000000002"
    assert command.effects[0].effect_type == "open_code_changeset"
    effect_insert = next(
        args
        for query, args in conn.statements
        if "INSERT INTO agent_approval_effects" in query
    )
    persisted_key = effect_insert[9]
    assert persisted_key == f"{command.command_id}:{command.effects[0].effect_id}"
    audit_types = [
        args[1]
        for query, args in conn.statements
        if "INSERT INTO agent_audit_log" in query
    ]
    assert audit_types == [
        "human_approval",
        "approval_effect_planned",
        "approval_command_queued",
    ]
    assert any(
        "SET status = 'approval_queued'" in query for query, _ in conn.statements
    )


@pytest.mark.asyncio
async def test_identical_command_retry_returns_the_persisted_command() -> None:
    conn = _CommandConn()
    kwargs = {
        "run_id": "run-1",
        "project_id": "demo",
        "actor_credential_id": "test-agents",
        "codegen_changeset_capability": "available",
        "decisions": (ApprovalDecision("p1", True),),
        "comment": "ship it",
    }

    first = await enqueue_approval_command(_Pool(conn), **kwargs)
    second = await enqueue_approval_command(_Pool(conn), **kwargs)

    assert second.command_id == first.command_id
    assert second.effects == first.effects
    assert (
        sum(
            "INSERT INTO agent_approval_commands" in query
            for query, _ in conn.statements
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["decision", "safety_result"])
async def test_enqueue_rejects_incomplete_persisted_gate_metadata(
    missing_field: str,
) -> None:
    conn = _CommandConn()
    del conn.gate_item[missing_field]

    with pytest.raises(
        ApprovalGateConflictError, match="canonical decision|safety result"
    ):
        await enqueue_approval_command(
            _Pool(conn),
            run_id="run-1",
            project_id="demo",
            actor_credential_id="test-agents",
            codegen_changeset_capability="available",
            decisions=(ApprovalDecision("p1", True),),
            comment=None,
        )

    assert not any(
        "INSERT INTO agent_approval_commands" in query for query, _ in conn.statements
    )


@pytest.mark.asyncio
async def test_enqueue_rejects_non_exact_gate_decisions_before_mutation() -> None:
    conn = _CommandConn()
    with pytest.raises(ApprovalDecisionError, match="exactly match"):
        await enqueue_approval_command(
            _Pool(conn),
            run_id="run-1",
            project_id="demo",
            actor_credential_id="test-agents",
            codegen_changeset_capability="available",
            decisions=(ApprovalDecision("unknown", True),),
            comment=None,
        )
    assert not any(
        "INSERT INTO agent_approval_commands" in query for query, _ in conn.statements
    )


@pytest.mark.asyncio
async def test_mandatory_audit_failure_prevents_command_return() -> None:
    conn = _CommandConn(fail_audit=True)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await enqueue_approval_command(
            _Pool(conn),
            run_id="run-1",
            project_id="demo",
            actor_credential_id="test-agents",
            codegen_changeset_capability="available",
            decisions=(ApprovalDecision("p1", True),),
            comment=None,
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_codegen_effect_before_any_command_is_persisted() -> None:
    conn = _CommandConn()

    with pytest.raises(ApprovalCapabilityError, match="changeset creation is disabled"):
        await enqueue_approval_command(
            _Pool(conn),
            run_id="run-1",
            project_id="demo",
            actor_credential_id="test-agents",
            codegen_changeset_capability="disabled",
            decisions=(ApprovalDecision("p1", True),),
            comment=None,
        )

    assert not any(
        "INSERT INTO agent_approval_commands" in query for query, _ in conn.statements
    )


@pytest.mark.asyncio
async def test_enqueue_allows_rejection_when_codegen_is_disabled() -> None:
    conn = _CommandConn()

    command = await enqueue_approval_command(
        _Pool(conn),
        run_id="run-1",
        project_id="demo",
        actor_credential_id="test-agents",
        codegen_changeset_capability="disabled",
        decisions=(ApprovalDecision("p1", False),),
        comment="Codegen publication is intentionally offline.",
    )

    assert command.approved_count == 0
    assert command.rejected_count == 1
    assert [effect.effect_type for effect in command.effects] == [
        "record_proposal_rejection"
    ]


def _effect(effect_type: str, *, quota: str) -> _ClaimedEffect:
    return _ClaimedEffect(
        effect_id="22222222-2222-4222-8222-222222222222",
        command_id="11111111-1111-4111-8111-111111111111",
        run_id="run-1",
        project_id="demo",
        item_id="p1",
        effect_type=effect_type,
        payload={
            "proposal_id": "p1",
            "title": "Durable change",
            "spec": "Implement it.",
            "decision": "approve",
            "safety_result": {"passed": True},
        },
        idempotency_key="11111111-1111-4111-8111-111111111111:22222222-2222-4222-8222-222222222222",
        quota_action_type=quota,
        attempt_count=1,
        max_attempts=8,
        lease_owner_id="worker-1",
    )


@pytest.mark.asyncio
async def test_worker_reserves_quota_before_codegen_and_passes_persisted_key(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []
    effect = _effect("open_code_changeset", quota="open_pull_request")

    async def fake_reserve(pool: Any, **kwargs: Any) -> object:
        calls.append(("reserve", kwargs["idempotency_key"]))
        return object()

    async def fake_open(**kwargs: Any) -> dict[str, str]:
        calls.append(("codegen", kwargs["idempotency_key"]))
        return {"changeset_id": "cs-1"}

    async def fake_mark(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(approval_effects, "reserve_mutation", fake_reserve)
    monkeypatch.setattr(approval_effects, "open_changeset", fake_open)
    monkeypatch.setattr(approval_effects, "mark_implemented", fake_mark)

    result = await _execute_effect(object(), effect)

    assert result == {"changeset_id": "cs-1"}
    assert calls == [
        ("reserve", effect.idempotency_key),
        ("codegen", effect.idempotency_key),
    ]


@pytest.mark.asyncio
async def test_worker_reserves_quota_before_config_and_passes_persisted_key(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []
    effect = _effect("stage_experiment_draft", quota="create_experiment")
    object.__setattr__(
        effect,
        "payload",
        {"experiment_id": "exp-1"},
    )
    object.__setattr__(effect, "item_id", "exp-1")

    async def fake_reserve(pool: Any, **kwargs: Any) -> object:
        calls.append(("reserve", kwargs["idempotency_key"]))
        return object()

    async def fake_stage(
        project_id: str, payload: dict[str, Any], *, idempotency_key: str
    ) -> None:
        calls.append(("config", idempotency_key))

    async def fake_record(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(approval_effects, "reserve_mutation", fake_reserve)
    monkeypatch.setattr(approval_effects, "stage_experiment_draft", fake_stage)
    monkeypatch.setattr(approval_effects, "record_designed_experiment", fake_record)

    result = await _execute_effect(object(), effect)

    assert result == {"experiment_id": "exp-1", "status": "drafted"}
    assert calls == [
        ("reserve", effect.idempotency_key),
        ("config", effect.idempotency_key),
    ]


def test_http_handler_has_no_process_local_effect_symbols() -> None:
    assert "BackgroundTasks" not in approvals.__dict__
    assert "run_supervisor" not in approvals.__dict__
    assert "open_changeset" not in approvals.__dict__
    assert "stage_experiment_draft" not in approvals.__dict__


@pytest.mark.asyncio
async def test_malformed_claimed_payload_is_terminalized_by_worker(monkeypatch) -> None:
    malformed = _effect("open_code_changeset", quota="open_pull_request")
    object.__setattr__(malformed, "payload", ["not", "an", "object"])
    failures: list[Exception] = []

    async def fake_claim(*args: Any, **kwargs: Any) -> _ClaimedEffect:
        return malformed

    async def fake_fail(pool: Any, effect: _ClaimedEffect, exc: Exception) -> None:
        failures.append(exc)

    monkeypatch.setattr(approval_effects, "_claim_effect", fake_claim)
    monkeypatch.setattr(approval_effects, "_fail_effect", fake_fail)

    assert await process_one_approval_effect(object(), owner_id="worker-1") is True
    assert len(failures) == 1
    assert isinstance(failures[0], PermanentApprovalEffectError)


@pytest.mark.parametrize(
    "status_code, error_type",
    [
        (400, PermanentApprovalEffectError),
        (409, PermanentApprovalEffectError),
        (422, PermanentApprovalEffectError),
        (429, RetryableApprovalEffectError),
        (500, RetryableApprovalEffectError),
        (503, RetryableApprovalEffectError),
    ],
)
def test_downstream_status_classification(status_code, error_type) -> None:
    request = httpx.Request("POST", "http://downstream/v1/mutate")
    response = httpx.Response(status_code, request=request)
    error = httpx.HTTPStatusError(
        "downstream failed",
        request=request,
        response=response,
    )

    assert isinstance(_classify_effect_error(error), error_type)


def test_network_failures_remain_retryable() -> None:
    request = httpx.Request("POST", "http://downstream/v1/mutate")
    error = httpx.ConnectError("connection refused", request=request)
    assert isinstance(_classify_effect_error(error), RetryableApprovalEffectError)
