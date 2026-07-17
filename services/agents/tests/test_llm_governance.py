"""Governed LLM routing, privacy, audit, and replica-safe budget contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.llm import router
from app.llm.contracts import (
    LlmBudgetExceededError,
    LlmGovernanceUnavailableError,
    LlmRequestContext,
    LlmRunInactiveError,
    PreparedLlmAttempt,
    ProjectLlmPolicy,
    ProviderPolicy,
)
from app.store.llm_governance import (
    begin_llm_call,
    prepare_provider_attempt,
    reconcile_orphaned_llm_attempts,
)


def _context(*, classification: str = "confidential", pool: Any = None):
    return LlmRequestContext(
        pool=pool or object(),
        project_id="projectA",
        run_id="run1",
        execution_kind="agent_run",
        purpose="agent.test.reason",
        data_classification=classification,
        execution_owner_id="worker-1",
    )


def _provider(
    name: str,
    model: str,
    *,
    classifications: frozenset[str] = frozenset({"confidential"}),
) -> ProviderPolicy:
    return ProviderPolicy(
        provider=name,
        model=model,
        endpoint_url=f"https://{name}.example/v1",
        data_residency="ca",
        allowed_data_classifications=classifications,
        input_cost_per_million_tokens_usd_micros=1_000_000,
        output_cost_per_million_tokens_usd_micros=1_000_000,
    )


def _candidate(name: str, model: str) -> dict[str, str]:
    return {
        "provider": name,
        "model": model,
        "endpoint_url": f"https://{name}.example/v1",
    }


def _policy(
    *providers: ProviderPolicy,
    cross_vendor: bool = False,
) -> ProjectLlmPolicy:
    return ProjectLlmPolicy(
        project_id="projectA",
        required_data_residency="ca",
        allow_cross_vendor_retry=cross_vendor,
        project_daily_cost_limit_usd_micros=1_000_000,
        run_cost_limit_usd_micros=1_000_000,
        providers=providers,
    )


@dataclass
class _GovernanceRecorder:
    policy: ProjectLlmPolicy
    events: list[str] = field(default_factory=list)
    attempt_finishes: list[dict[str, Any]] = field(default_factory=list)
    call_finishes: list[dict[str, Any]] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expected_call_id = uuid4()

        async def load(context):
            self.events.append("policy")
            return self.policy

        async def begin(context, *, prompt_sha256):
            assert len(prompt_sha256) == 64
            self.events.append("call:prepared")
            return expected_call_id

        async def prepare(
            context,
            *,
            call_id,
            attempt_number,
            provider,
            model,
            endpoint_url,
            prompt_sha256,
            estimated_input_tokens,
            max_output_tokens,
        ):
            del prompt_sha256, estimated_input_tokens, max_output_tokens
            assert call_id == expected_call_id
            policy = self.policy.provider_policy(
                context, provider, model, endpoint_url
            )
            assert policy is not None
            self.events.append(f"attempt:{attempt_number}:prepared:{provider}/{model}")
            return PreparedLlmAttempt(
                attempt_id=uuid4(),
                reserved_cost_usd_micros=10_000,
                provider_policy=policy,
            )

        async def mark(context, *, attempt_id):
            assert isinstance(attempt_id, UUID)
            self.events.append("attempt:in_flight")

        async def finish_attempt(context, **kwargs):
            self.attempt_finishes.append(kwargs)
            self.events.append(f"attempt:{kwargs['status']}")
            return 123

        async def block_attempt(context, **kwargs):
            self.attempt_finishes.append({"status": "blocked", **kwargs})
            self.events.append("attempt:blocked")

        async def finish_call(context, **kwargs):
            self.call_finishes.append(kwargs)
            self.events.append(f"call:{kwargs['status']}")

        monkeypatch.setattr(router, "load_project_llm_policy", load)
        monkeypatch.setattr(router, "begin_llm_call", begin)
        monkeypatch.setattr(router, "prepare_provider_attempt", prepare)
        monkeypatch.setattr(router, "mark_provider_egress", mark)
        monkeypatch.setattr(router, "finish_provider_attempt", finish_attempt)
        monkeypatch.setattr(
            router, "block_provider_attempt_before_egress", block_attempt
        )
        monkeypatch.setattr(router, "finish_llm_call", finish_call)


_MESSAGES = [
    {"role": "system", "content": "system"},
    {"role": "user", "content": "tenant data"},
]


def test_anthropic_usage_includes_prompt_cache_tokens():
    usage = type(
        "Usage",
        (),
        {
            "input_tokens": 11,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 5,
            "output_tokens": 3,
        },
    )()

    assert router._anthropic_usage_tokens(usage) == (23, 3)


def test_google_usage_includes_tool_prompt_and_thought_tokens():
    usage = type(
        "UsageMetadata",
        (),
        {
            "prompt_token_count": 11,
            "tool_use_prompt_token_count": 7,
            "candidates_token_count": 5,
            "thoughts_token_count": 3,
        },
    )()

    assert router._google_usage_tokens(usage) == (18, 8)


@pytest.mark.asyncio
async def test_actual_usage_is_audited_before_plain_content_is_returned(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [_candidate("openai", "model-a")],
    )

    async def invoke(model, messages, **kwargs):
        recorder.events.append("provider:egress")
        return router.TextCompletion("answer", input_tokens=11, output_tokens=7)

    monkeypatch.setitem(router._PROVIDER_FN, "openai", invoke)

    answer = await router.chat_completion("fast", _MESSAGES, context=_context())

    assert answer == "answer"
    assert recorder.attempt_finishes[0]["input_tokens"] == 11
    assert recorder.attempt_finishes[0]["output_tokens"] == 7
    assert recorder.events.index("call:succeeded") > recorder.events.index(
        "attempt:succeeded"
    )


@pytest.mark.asyncio
async def test_unknown_provider_error_is_nonretryable(monkeypatch):
    openai_policy = _provider("openai", "model-a")
    anthropic_policy = _provider("anthropic", "model-b")
    recorder = _GovernanceRecorder(
        _policy(openai_policy, anthropic_policy, cross_vendor=True)
    )
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [
            _candidate("openai", "model-a"),
            _candidate("anthropic", "model-b"),
        ],
    )
    invoked: list[str] = []

    async def unknown(model, messages, **kwargs):
        invoked.append("openai")
        raise ArithmeticError("unexpected provider failure")

    async def must_not_run(model, messages, **kwargs):
        invoked.append("anthropic")
        return router.TextCompletion("unsafe fallback")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", unknown)
    monkeypatch.setitem(router._PROVIDER_FN, "anthropic", must_not_run)

    with pytest.raises(RuntimeError, match="without a safe retry"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked == ["openai"]
    assert recorder.attempt_finishes[0]["error_classification"] == "unknown"
    assert recorder.attempt_finishes[0]["retryable"] is False
    assert recorder.call_finishes[-1]["error_classification"] == "unknown"


@pytest.mark.asyncio
async def test_cross_vendor_retry_is_denied_by_default(monkeypatch):
    openai_policy = _provider("openai", "model-a")
    anthropic_policy = _provider("anthropic", "model-b")
    recorder = _GovernanceRecorder(_policy(openai_policy, anthropic_policy))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [
            _candidate("openai", "model-a"),
            _candidate("anthropic", "model-b"),
        ],
    )
    invoked: list[str] = []

    async def timeout(model, messages, **kwargs):
        invoked.append("openai")
        raise TimeoutError("provider timed out")

    async def must_not_run(model, messages, **kwargs):
        invoked.append("anthropic")
        return router.TextCompletion("unsafe fallback")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", timeout)
    monkeypatch.setitem(router._PROVIDER_FN, "anthropic", must_not_run)

    with pytest.raises(RuntimeError, match="No safe LLM retry remained"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked == ["openai"]
    assert recorder.attempt_finishes[0]["retryable"] is True


@pytest.mark.asyncio
async def test_cross_vendor_retry_requires_explicit_policy_for_both_models(monkeypatch):
    openai_policy = _provider("openai", "model-a")
    anthropic_policy = _provider("anthropic", "model-b")
    recorder = _GovernanceRecorder(
        _policy(openai_policy, anthropic_policy, cross_vendor=True)
    )
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [
            _candidate("openai", "model-a"),
            _candidate("anthropic", "model-b"),
        ],
    )
    invoked: list[str] = []

    async def timeout(model, messages, **kwargs):
        invoked.append("openai")
        raise TimeoutError("provider timed out")

    async def fallback(model, messages, **kwargs):
        invoked.append("anthropic")
        return router.TextCompletion("safe fallback", 3, 2)

    monkeypatch.setitem(router._PROVIDER_FN, "openai", timeout)
    monkeypatch.setitem(router._PROVIDER_FN, "anthropic", fallback)

    result = await router.chat_completion("fast", _MESSAGES, context=_context())

    assert result == "safe fallback"
    assert invoked == ["openai", "anthropic"]
    assert [item["status"] for item in recorder.attempt_finishes] == [
        "failed",
        "succeeded",
    ]


@pytest.mark.asyncio
async def test_privacy_classification_denial_is_durably_blocked(monkeypatch):
    public_only = _provider("openai", "model-a", classifications=frozenset({"public"}))
    recorder = _GovernanceRecorder(_policy(public_only))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [_candidate("openai", "model-a")],
    )
    invoked = False

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("leaked")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(RuntimeError, match="permits none"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked is False
    assert recorder.call_finishes[-1]["status"] == "blocked"
    assert recorder.call_finishes[-1]["error_classification"] == "policy_denied"


@pytest.mark.asyncio
async def test_provider_policy_is_bound_to_exact_endpoint_before_egress(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [
            {
                "provider": "openai",
                "model": "model-a",
                "endpoint_url": "https://unapproved.example/v1",
            }
        ],
    )
    invoked = False

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("leaked")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(RuntimeError, match="permits none"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked is False
    assert not any(event.startswith("attempt:") for event in recorder.events)
    assert recorder.call_finishes[-1]["error_classification"] == "policy_denied"


@pytest.mark.asyncio
async def test_missing_provider_is_durably_blocked(monkeypatch):
    recorder = _GovernanceRecorder(_policy())
    recorder.install(monkeypatch)
    monkeypatch.setattr(router, "_tier_models", lambda tier: [])

    with pytest.raises(RuntimeError, match="No LLM providers are configured"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert recorder.call_finishes[-1]["status"] == "blocked"
    assert recorder.call_finishes[-1]["error_classification"] == "no_provider"


@pytest.mark.asyncio
async def test_budget_denial_terminalizes_logical_call_before_egress(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [_candidate("openai", "model-a")],
    )
    invoked = False

    async def deny_budget(context, **kwargs):
        raise LlmBudgetExceededError("run ceiling exhausted")

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("overspend")

    monkeypatch.setattr(router, "prepare_provider_attempt", deny_budget)
    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(LlmBudgetExceededError):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked is False
    assert recorder.call_finishes[-1]["status"] == "blocked"
    assert recorder.call_finishes[-1]["error_classification"] == "budget_exceeded"


@pytest.mark.asyncio
async def test_audit_failure_after_successful_egress_fails_closed(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [_candidate("openai", "model-a")],
    )
    egress = False

    async def invoke(model, messages, **kwargs):
        nonlocal egress
        egress = True
        return router.TextCompletion("must not escape", 2, 1)

    async def fail_audit(context, **kwargs):
        raise LlmGovernanceUnavailableError("postgres unavailable after egress")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", invoke)
    monkeypatch.setattr(router, "finish_provider_attempt", fail_audit)

    with pytest.raises(LlmGovernanceUnavailableError):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert egress is True
    assert not any(item["status"] == "succeeded" for item in recorder.call_finishes)


@pytest.mark.asyncio
async def test_cancellation_after_egress_is_persisted_and_reraised(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [_candidate("openai", "model-a")],
    )

    async def cancel(model, messages, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setitem(router._PROVIDER_FN, "openai", cancel)

    with pytest.raises(asyncio.CancelledError):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert recorder.attempt_finishes[-1]["status"] == "cancelled"
    assert recorder.call_finishes[-1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_tool_completion_uses_the_same_governance_path(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_tier_models",
        lambda tier: [_candidate("openai", "model-a")],
    )
    monkeypatch.setattr(router, "_get_openai", lambda: object())

    async def tool_invoke(*args, **kwargs):
        return router.ToolCompletion("tool answer", input_tokens=9, output_tokens=4)

    monkeypatch.setattr(router, "_openai_completion_tools", tool_invoke)

    result = await router.chat_completion_with_tools(
        "fast", _MESSAGES, tools=[], context=_context()
    )

    assert result.text == "tool answer"
    assert recorder.attempt_finishes[-1]["input_tokens"] == 9
    assert recorder.call_finishes[-1]["status"] == "succeeded"


@dataclass
class _BudgetBackend:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    calls: dict[UUID, str] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    advisory_locks: list[str] = field(default_factory=list)


class _Transaction:
    def __init__(self, backend: _BudgetBackend) -> None:
        self.backend = backend

    async def __aenter__(self):
        await self.backend.lock.acquire()

    async def __aexit__(self, *exc):
        self.backend.lock.release()
        return False


class _BudgetConn:
    def __init__(self, backend: _BudgetBackend) -> None:
        self.backend = backend

    def transaction(self):
        return _Transaction(self.backend)

    async def fetchrow(self, query: str, *args: Any):
        if "FROM agent_runs" in query:
            assert args == ("run1", "projectA", "worker-1")
            return {"active": 1}
        if "FROM llm_provider_attempts" in query:
            call_id = args[0]
            matching = [
                item for item in self.backend.attempts if item["call_id"] == call_id
            ]
            return max(matching, key=lambda item: item["attempt_number"], default=None)
        if "FROM llm_project_policies AS policy" in query:
            return {
                "required_data_residency": "ca",
                "allow_cross_vendor_retry": False,
                "project_daily_cost_limit_usd_micros": 10,
                "run_cost_limit_usd_micros": 10,
                "provider": "openai",
                "model": "model-a",
                "endpoint_url": "https://openai.example/v1",
                "data_residency": "ca",
                "allowed_data_classifications": ["confidential"],
                "input_cost_per_million_tokens_usd_micros": 1_000_000,
                "output_cost_per_million_tokens_usd_micros": 0,
            }
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: Any):
        if "SELECT status" in query and "FROM llm_calls" in query:
            return self.backend.calls.get(args[0])
        if "SELECT COALESCE(sum" in query:
            return sum(item["reserved_cost"] for item in self.backend.attempts)
        raise AssertionError(query)

    async def execute(self, query: str, *args: Any):
        if "pg_advisory_xact_lock" in query:
            self.backend.advisory_locks.append(str(args[0]))
            return "SELECT 1"
        if "INSERT INTO llm_provider_attempts" in query:
            self.backend.attempts.append(
                {
                    "call_id": args[1],
                    "attempt_number": args[4],
                    "provider": args[5],
                    "status": "prepared",
                    "retryable": False,
                    "reserved_cost": args[12],
                }
            )
            return "INSERT 0 1"
        if "UPDATE llm_calls" in query:
            self.backend.calls[args[0]] = "in_flight"
            return "UPDATE 1"
        raise AssertionError(query)


class _Acquire:
    def __init__(self, connection: _BudgetConn) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exc):
        return False


class _BudgetPool:
    def __init__(self, backend: _BudgetBackend) -> None:
        self.connection = _BudgetConn(backend)

    def acquire(self):
        return _Acquire(self.connection)


@pytest.mark.asyncio
async def test_concurrent_replicas_cannot_race_past_shared_cost_ceiling():
    backend = _BudgetBackend()
    call_a = uuid4()
    call_b = uuid4()
    backend.calls = {call_a: "prepared", call_b: "prepared"}
    pool_a = _BudgetPool(backend)
    pool_b = _BudgetPool(backend)

    async def reserve(pool: Any, call_id: UUID):
        return await prepare_provider_attempt(
            _context(pool=pool),
            call_id=call_id,
            attempt_number=1,
            provider="openai",
            model="model-a",
            endpoint_url="https://openai.example/v1",
            prompt_sha256="a" * 64,
            estimated_input_tokens=10,
            max_output_tokens=1,
        )

    results = await asyncio.gather(
        reserve(pool_a, call_a), reserve(pool_b, call_b), return_exceptions=True
    )

    assert sum(isinstance(item, PreparedLlmAttempt) for item in results) == 1
    assert sum(isinstance(item, LlmBudgetExceededError) for item in results) == 1
    assert len(backend.attempts) == 1
    assert backend.advisory_locks.count("apdl:llm-budget:project:projectA") == 2
    assert backend.advisory_locks.count("apdl:llm-budget:run:projectA:run1") == 2


class _ReconcileTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _ReconcileConn:
    def __init__(self) -> None:
        self.prepared_call = uuid4()
        self.in_flight_call = uuid4()
        self.locked = False

    def transaction(self):
        return _ReconcileTransaction()

    async def execute(self, query: str, *args: Any):
        assert "pg_advisory_xact_lock" in query
        assert args == ("apdl:llm-attempt-reconciliation",)
        self.locked = True
        return "SELECT 1"

    async def fetch(self, query: str, *args: Any):
        assert "UPDATE llm_provider_attempts" in query
        assert "Owning execution ended before provider egress" in query
        assert "attempt.reserved_cost_usd_micros" in query
        return [
            {"call_id": self.prepared_call, "previous_status": "prepared"},
            {"call_id": self.in_flight_call, "previous_status": "in_flight"},
        ]

    async def fetchval(self, query: str, *args: Any):
        assert "UPDATE llm_calls" in query
        assert "orphaned_calls" in query
        assert args == ()
        return 2


class _ReconcilePool:
    def __init__(self, conn: _ReconcileConn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _InactiveOwnerConn:
    def transaction(self):
        return _ReconcileTransaction()

    async def fetchrow(self, query: str, *args: Any):
        assert "lease_owner_id = $3" in query
        assert args == ("run1", "projectA", "worker-stale")
        return None


@pytest.mark.asyncio
async def test_stale_supervisor_owner_cannot_begin_a_logical_call():
    context = LlmRequestContext(
        pool=_ReconcilePool(_InactiveOwnerConn()),
        project_id="projectA",
        run_id="run1",
        execution_kind="agent_run",
        purpose="agent.test.reason",
        data_classification="confidential",
        execution_owner_id="worker-stale",
    )

    with pytest.raises(LlmRunInactiveError, match="is not active"):
        await begin_llm_call(context, prompt_sha256="a" * 64)


@pytest.mark.asyncio
async def test_orphan_reconciliation_releases_only_pre_egress_reservations():
    conn = _ReconcileConn()

    result = await reconcile_orphaned_llm_attempts(_ReconcilePool(conn))

    assert conn.locked is True
    assert result.prepared_blocked == 1
    assert result.in_flight_cancelled == 1
    assert result.calls_cancelled == 2
