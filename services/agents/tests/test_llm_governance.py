"""Governed LLM routing, privacy, audit, and replica-safe budget contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.llm import router
from app.llm.client_cache import (
    OwnedProviderClient,
    ProviderClientCache,
    ProviderClientRetiredError,
)
from app.llm.contracts import (
    LlmBudgetExceededError,
    LlmCredentialUnavailableError,
    LlmGovernanceUnavailableError,
    LlmRequestContext,
    LlmRunInactiveError,
    PreparedLlmAttempt,
    ProjectModelAssignment,
    ProjectLlmPolicy,
    ProviderPolicy,
)
from app.store.llm_governance import (
    begin_llm_call,
    mark_provider_egress,
    prepare_provider_attempt,
    reconcile_orphaned_llm_attempts,
)
from app.store.llm_credentials import DecryptedCredential


class _ProviderLease:
    def __init__(self, client: Any, events: list[str]) -> None:
        self.client = client
        self._events = events

    async def retire(self) -> None:
        self._events.append("client:retired")

    async def release(self) -> None:
        self._events.append("client:released")


def _context(
    *,
    classification: str = "confidential",
    pool: Any = None,
    llm_runtime: Any = None,
):
    return LlmRequestContext(
        pool=pool or object(),
        llm_runtime=llm_runtime if llm_runtime is not None else object(),
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
        state="active",
        version=7,
    )


@dataclass
class _GovernanceRecorder:
    policy: ProjectLlmPolicy
    events: list[str] = field(default_factory=list)
    attempt_finishes: list[dict[str, Any]] = field(default_factory=list)
    call_finishes: list[dict[str, Any]] = field(default_factory=list)
    provider_client: Any = field(default_factory=object)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expected_call_id = uuid4()

        async def load(context):
            self.events.append("policy")
            return self.policy

        async def begin(context, *, prompt_sha256):
            assert len(prompt_sha256) == 64
            self.events.append("call:prepared")
            return expected_call_id

        async def assignment(context, *, tier):
            del context
            if not self.policy.providers:
                raise LlmCredentialUnavailableError(
                    f"Project has no {tier} LLM model assignment"
                )
            selected = self.policy.providers[0]
            return ProjectModelAssignment(
                project_id=self.policy.project_id,
                tier=tier,
                provider=selected.provider,
                model=selected.model,
                endpoint_url=selected.endpoint_url,
                setup_version=self.policy.version,
                connection_version=3,
                inventory_version=5,
                model_catalog_version="llm-provider-catalog@2",
            )

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
            model_tier,
            setup_version,
            connection_version,
            inventory_version,
            model_catalog_version,
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
                credential_id=uuid4() if provider != "local" else None,
                credential_version=1 if provider != "local" else None,
                setup_version=setup_version,
                model_tier=model_tier,
                connection_version=connection_version,
                inventory_version=inventory_version,
                model_catalog_version=model_catalog_version,
            )

        async def load_key(context, prepared):
            del context, prepared
            self.events.append("credential:loaded")
            return "project-provider-key"

        async def acquire_client(context, prepared, api_key):
            del context, prepared
            assert api_key == "project-provider-key"
            self.events.append("client:leased")
            return _ProviderLease(self.provider_client, self.events)

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
        monkeypatch.setattr(router, "load_project_model_assignment", assignment)
        monkeypatch.setattr(router, "begin_llm_call", begin)
        monkeypatch.setattr(router, "prepare_provider_attempt", prepare)
        monkeypatch.setattr(router, "_load_attempt_api_key", load_key)
        monkeypatch.setattr(router, "_acquire_provider_client", acquire_client)
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
async def test_exact_client_is_reused_without_bypassing_vault_revalidation():
    credential_id = UUID("10000000-0000-4000-8000-000000000001")
    vault_calls: list[tuple[UUID, int]] = []
    created: list[object] = []
    closed: list[object] = []

    class Vault:
        async def load_active(self, project_id, provider, **kwargs):
            assert (project_id, provider) == ("projectA", "openai")
            vault_calls.append(
                (kwargs["credential_id"], kwargs["credential_version"])
            )
            return DecryptedCredential(
                credential_id=kwargs["credential_id"],
                project_id=project_id,
                provider=provider,
                credential_version=kwargs["credential_version"],
                api_key=f"secret-{kwargs['credential_version']}",
            )

    async def factory(identity, api_key):
        client = object()
        created.append(client)

        async def close():
            closed.append(client)

        assert api_key == f"secret-{identity.credential_version}"
        return OwnedProviderClient(client, close)

    cache = ProviderClientCache(max_entries=2, factory=factory)
    runtime = SimpleNamespace(credential_store=Vault(), provider_clients=cache)
    context = LlmRequestContext(
        pool=object(),
        llm_runtime=runtime,
        project_id="projectA",
        run_id="run1",
        execution_kind="agent_run",
        purpose="agent.test.reason",
        data_classification="confidential",
        execution_owner_id="worker-1",
    )
    policy = _provider("openai", "model-a")

    def prepared(version: int) -> PreparedLlmAttempt:
        return PreparedLlmAttempt(
            attempt_id=uuid4(),
            reserved_cost_usd_micros=10_000,
            provider_policy=policy,
            credential_id=credential_id,
            credential_version=version,
            setup_version=7,
            model_tier="fast",
            connection_version=version,
            inventory_version=5,
            model_catalog_version="llm-provider-catalog@2",
        )

    first_attempt = prepared(1)
    first_key = await router._load_attempt_api_key(context, first_attempt)
    first = await router._acquire_provider_client(context, first_attempt, first_key)
    first_client = first.client
    await first.release()

    repeated_attempt = prepared(1)
    repeated_key = await router._load_attempt_api_key(context, repeated_attempt)
    repeated = await router._acquire_provider_client(
        context, repeated_attempt, repeated_key
    )
    assert repeated.client is first_client
    await repeated.release()

    rotated_attempt = prepared(2)
    rotated_key = await router._load_attempt_api_key(context, rotated_attempt)
    rotated = await router._acquire_provider_client(
        context, rotated_attempt, rotated_key
    )

    assert rotated.client is not first_client
    assert vault_calls == [(credential_id, 1), (credential_id, 1), (credential_id, 2)]
    assert len(created) == 2
    assert closed == [first_client]

    await rotated.release()
    await cache.aclose()


@pytest.mark.asyncio
async def test_actual_usage_is_audited_before_plain_content_is_returned(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
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
    assert recorder.events.index("credential:loaded") < recorder.events.index(
        "client:leased"
    )
    assert recorder.events.index("client:leased") < recorder.events.index(
        "attempt:in_flight"
    )
    assert recorder.events.index("attempt:in_flight") < recorder.events.index(
        "provider:egress"
    )


@pytest.mark.asyncio
async def test_xai_plain_completion_uses_the_governed_provider_path(monkeypatch):
    provider = _provider("xai", "grok-reviewed")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    class Completions:
        async def create(self, **kwargs):
            assert kwargs["model"] == "grok-reviewed"
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "message": type(
                                    "Message", (), {"content": "grok answer"}
                                )()
                            },
                        )()
                    ],
                    "usage": type(
                        "Usage",
                        (),
                        {"prompt_tokens": 13, "completion_tokens": 5},
                    )(),
                },
            )()

    client = type(
        "Client",
        (),
        {
            "chat": type(
                "Chat",
                (),
                {"completions": Completions()},
            )()
        },
    )()
    recorder.provider_client = client

    answer = await router.chat_completion("reasoning", _MESSAGES, context=_context())

    assert answer == "grok answer"
    assert "attempt:1:prepared:xai/grok-reviewed" in recorder.events
    assert recorder.attempt_finishes[0]["input_tokens"] == 13
    assert recorder.attempt_finishes[0]["output_tokens"] == 5
    assert recorder.call_finishes[-1]["status"] == "succeeded"
    assert "client:leased" in recorder.events
    assert "client:released" in recorder.events


@pytest.mark.asyncio
async def test_unknown_provider_error_is_nonretryable(monkeypatch):
    openai_policy = _provider("openai", "model-a")
    anthropic_policy = _provider("anthropic", "model-b")
    recorder = _GovernanceRecorder(
        _policy(openai_policy, anthropic_policy, cross_vendor=True)
    )
    recorder.install(monkeypatch)
    invoked: list[str] = []

    async def unknown(model, messages, **kwargs):
        invoked.append("openai")
        raise ArithmeticError("unexpected provider failure")

    async def must_not_run(model, messages, **kwargs):
        invoked.append("anthropic")
        return router.TextCompletion("unsafe fallback")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", unknown)
    monkeypatch.setitem(router._PROVIDER_FN, "anthropic", must_not_run)

    with pytest.raises(RuntimeError, match="LLM call failed"):
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
    invoked: list[str] = []

    async def timeout(model, messages, **kwargs):
        invoked.append("openai")
        raise TimeoutError("provider timed out")

    async def must_not_run(model, messages, **kwargs):
        invoked.append("anthropic")
        return router.TextCompletion("unsafe fallback")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", timeout)
    monkeypatch.setitem(router._PROVIDER_FN, "anthropic", must_not_run)

    with pytest.raises(RuntimeError, match="LLM call failed"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked == ["openai"]
    assert recorder.attempt_finishes[0]["retryable"] is False


@pytest.mark.asyncio
async def test_cross_vendor_fallback_is_never_implicit_even_when_policy_allows(
    monkeypatch,
):
    openai_policy = _provider("openai", "model-a")
    anthropic_policy = _provider("anthropic", "model-b")
    recorder = _GovernanceRecorder(
        _policy(openai_policy, anthropic_policy, cross_vendor=True)
    )
    recorder.install(monkeypatch)
    invoked: list[str] = []

    async def timeout(model, messages, **kwargs):
        invoked.append("openai")
        raise TimeoutError("provider timed out")

    async def fallback(model, messages, **kwargs):
        invoked.append("anthropic")
        return router.TextCompletion("safe fallback", 3, 2)

    monkeypatch.setitem(router._PROVIDER_FN, "openai", timeout)
    monkeypatch.setitem(router._PROVIDER_FN, "anthropic", fallback)

    with pytest.raises(RuntimeError, match="LLM call failed"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked == ["openai"]
    assert [item["status"] for item in recorder.attempt_finishes] == ["failed"]
    assert recorder.attempt_finishes[0]["retryable"] is False


@pytest.mark.asyncio
async def test_privacy_classification_denial_is_durably_blocked(monkeypatch):
    public_only = _provider("openai", "model-a", classifications=frozenset({"public"}))
    recorder = _GovernanceRecorder(_policy(public_only))
    recorder.install(monkeypatch)
    invoked = False

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("leaked")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(RuntimeError, match="does not permit"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked is False
    assert recorder.call_finishes[-1]["status"] == "blocked"
    assert recorder.call_finishes[-1]["error_classification"] == "policy_denied"


@pytest.mark.asyncio
async def test_provider_policy_is_bound_to_exact_endpoint_before_egress(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)

    async def unapproved_assignment(context, *, tier):
        del context
        return ProjectModelAssignment(
            project_id="projectA",
            tier=tier,
            provider="openai",
            model="model-a",
            endpoint_url="https://unapproved.example/v1",
            setup_version=7,
            connection_version=3,
            inventory_version=5,
            model_catalog_version="llm-provider-catalog@2",
        )

    monkeypatch.setattr(
        router,
        "load_project_model_assignment",
        unapproved_assignment,
    )
    invoked = False

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("leaked")

    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(RuntimeError, match="does not permit"):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked is False
    assert not any(event.startswith("attempt:") for event in recorder.events)
    assert recorder.call_finishes[-1]["error_classification"] == "policy_denied"


@pytest.mark.asyncio
async def test_missing_provider_is_durably_blocked(monkeypatch):
    recorder = _GovernanceRecorder(_policy())
    recorder.install(monkeypatch)
    with pytest.raises(
        LlmCredentialUnavailableError,
        match="no fast LLM model assignment",
    ):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert recorder.call_finishes[-1]["status"] == "blocked"
    assert (
        recorder.call_finishes[-1]["error_classification"]
        == "credential_unavailable"
    )


@pytest.mark.asyncio
async def test_budget_denial_terminalizes_logical_call_before_egress(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
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
async def test_egress_mark_failure_retires_the_prepared_client(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
    invoked = False

    async def fail_mark(context, *, attempt_id):
        del context, attempt_id
        raise LlmGovernanceUnavailableError("egress authorization unavailable")

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("unsafe")

    monkeypatch.setattr(router, "mark_provider_egress", fail_mark)
    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(LlmGovernanceUnavailableError):
        await router.chat_completion("fast", _MESSAGES, context=_context())

    assert invoked is False
    assert recorder.events.index("client:leased") < recorder.events.index(
        "client:retired"
    )
    assert recorder.events.index("client:retired") < recorder.events.index(
        "client:released"
    )
    assert recorder.attempt_finishes[-1]["status"] == "blocked"
    assert (
        recorder.call_finishes[-1]["error_classification"]
        == "governance_unavailable"
    )


@pytest.mark.asyncio
async def test_superseded_client_identity_is_durably_credential_unavailable(
    monkeypatch,
):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    acquire_provider_client = router._acquire_provider_client
    recorder.install(monkeypatch)
    monkeypatch.setattr(
        router,
        "_acquire_provider_client",
        acquire_provider_client,
    )
    invoked = False

    class SupersededCache:
        async def acquire(self, identity, api_key):
            assert identity.connection_version == 3
            assert api_key == "project-provider-key"
            raise ProviderClientRetiredError("stale identity")

    async def must_not_run(model, messages, **kwargs):
        nonlocal invoked
        invoked = True
        return router.TextCompletion("unsafe")

    runtime = SimpleNamespace(provider_clients=SupersededCache())
    monkeypatch.setitem(router._PROVIDER_FN, "openai", must_not_run)

    with pytest.raises(LlmCredentialUnavailableError, match="superseded"):
        await router.chat_completion(
            "fast",
            _MESSAGES,
            context=_context(llm_runtime=runtime),
        )

    assert invoked is False
    assert recorder.attempt_finishes[-1]["status"] == "blocked"
    assert (
        recorder.attempt_finishes[-1]["error_classification"]
        == "credential_unavailable"
    )
    assert (
        recorder.call_finishes[-1]["error_classification"]
        == "credential_unavailable"
    )


@pytest.mark.asyncio
async def test_audit_failure_after_successful_egress_fails_closed(monkeypatch):
    provider = _provider("openai", "model-a")
    recorder = _GovernanceRecorder(_policy(provider))
    recorder.install(monkeypatch)
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
    provider: str = "openai"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    calls: dict[UUID, str] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    advisory_locks: list[str] = field(default_factory=list)
    policy_queries: list[str] = field(default_factory=list)


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
            self.backend.policy_queries.append(query)
            provider = self.backend.provider
            return {
                "required_data_residency": "ca",
                "allow_cross_vendor_retry": False,
                "project_daily_cost_limit_usd_micros": 10,
                "run_cost_limit_usd_micros": 10,
                "provider": provider,
                "model": "model-a",
                "endpoint_url": f"https://{provider}.example/v1",
                "data_residency": "ca",
                "allowed_data_classifications": ["confidential"],
                "input_cost_per_million_tokens_usd_micros": 1_000_000,
                "output_cost_per_million_tokens_usd_micros": 0,
                "credential_id": (
                    UUID("10000000-0000-4000-8000-000000000049")
                    if provider != "local"
                    else None
                ),
                "credential_version": 1 if provider != "local" else None,
            }
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: Any):
        if "SELECT state = 'active'" in query:
            assert args == ("projectA",)
            return True
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
                    "provider": args[6],
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
            model_tier="fast",
            setup_version=7,
            connection_version=3,
            inventory_version=5,
            model_catalog_version="llm-provider-catalog@2",
        )

    results = await asyncio.gather(
        reserve(pool_a, call_a), reserve(pool_b, call_b), return_exceptions=True
    )

    assert sum(isinstance(item, PreparedLlmAttempt) for item in results) == 1
    assert sum(isinstance(item, LlmBudgetExceededError) for item in results) == 1
    assert len(backend.attempts) == 1
    assert backend.advisory_locks.count("apdl:llm-budget:project:projectA") == 2
    assert backend.advisory_locks.count("apdl:llm-budget:run:projectA:run1") == 2
    assert backend.advisory_locks.count("apdl:agents-setup:projectA") == 2
    assert backend.advisory_locks.count("apdl:llm-vault:projectA:openai") == 2
    assert all("FOR SHARE OF policy" in query for query in backend.policy_queries)
    assert all(
        "FOR SHARE OF policy, provider" not in query
        for query in backend.policy_queries
    )


@pytest.mark.asyncio
async def test_local_attempt_uses_setup_lock_without_vault_pair_lock():
    backend = _BudgetBackend(provider="local")
    call_id = uuid4()
    backend.calls = {call_id: "prepared"}

    prepared = await prepare_provider_attempt(
        _context(pool=_BudgetPool(backend)),
        call_id=call_id,
        attempt_number=1,
        provider="local",
        model="model-a",
        endpoint_url="https://local.example/v1",
        prompt_sha256="a" * 64,
        estimated_input_tokens=1,
        max_output_tokens=1,
        model_tier="fast",
        setup_version=7,
        connection_version=3,
        inventory_version=5,
        model_catalog_version="llm-provider-catalog@2",
    )

    assert prepared.provider_policy.provider == "local"
    assert "apdl:agents-setup:projectA" in backend.advisory_locks
    assert not any(
        lock.startswith("apdl:llm-vault:") for lock in backend.advisory_locks
    )


class _ReconcileTransaction:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _EgressAuthorityConn:
    def __init__(
        self,
        *,
        authorized: bool = True,
        provider: str = "openai",
    ) -> None:
        self.authorized = authorized
        self.provider = provider
        self.queries: list[str] = []
        self.operations: list[tuple[str, str, tuple[Any, ...]]] = []

    def transaction(self) -> _ReconcileTransaction:
        return _ReconcileTransaction()

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append(query)
        self.operations.append(("fetchrow", query, args))
        assert "FROM agent_runs" in query
        return {"active": 1}

    async def fetchval(self, query: str, *args: Any):
        self.queries.append(query)
        self.operations.append(("fetchval", query, args))
        if "SELECT provider" in query and "FROM llm_provider_attempts" in query:
            return self.provider
        if "SELECT state = 'active'" in query:
            return True
        if "SELECT attempt.attempt_id" in query:
            assert "FOR UPDATE OF attempt" in query
            assert "FOR SHARE" not in query
            assert "FOR UPDATE OF policy" not in query
            return args[0] if self.authorized else None
        if "UPDATE llm_provider_attempts" in query:
            return args[0]
        raise AssertionError(query)

    async def execute(self, query: str, *args: Any):
        self.queries.append(query)
        self.operations.append(("execute", query, args))
        assert "pg_advisory_xact_lock" in query
        return "SELECT 1"


@pytest.mark.asyncio
async def test_provider_egress_serializes_unlocked_read_only_authority():
    attempt_id = uuid4()
    conn = _EgressAuthorityConn()

    await mark_provider_egress(
        _context(pool=_ReconcilePool(conn)),
        attempt_id=attempt_id,
    )

    setup_lock_index = next(
        index
        for index, (operation, _query, args) in enumerate(conn.operations)
        if operation == "execute" and args == ("apdl:agents-setup:projectA",)
    )
    pair_lock_index = next(
        index
        for index, (operation, _query, args) in enumerate(conn.operations)
        if operation == "execute"
        and args == ("apdl:llm-vault:projectA:openai",)
    )
    authority_read_index = next(
        index
        for index, (operation, query, _args) in enumerate(conn.operations)
        if operation == "fetchval" and "SELECT attempt.attempt_id" in query
    )
    assert setup_lock_index < pair_lock_index < authority_read_index
    assert any("UPDATE llm_provider_attempts" in query for query in conn.queries)


@pytest.mark.asyncio
async def test_local_provider_egress_skips_vault_pair_lock():
    conn = _EgressAuthorityConn(provider="local")

    await mark_provider_egress(
        _context(pool=_ReconcilePool(conn)),
        attempt_id=uuid4(),
    )

    lock_args = [
        args
        for operation, _query, args in conn.operations
        if operation == "execute"
    ]
    assert ("apdl:agents-setup:projectA",) in lock_args
    assert not any(
        args and str(args[0]).startswith("apdl:llm-vault:")
        for args in lock_args
    )


@pytest.mark.asyncio
async def test_provider_egress_stops_when_exact_authority_was_replaced():
    attempt_id = uuid4()
    conn = _EgressAuthorityConn(authorized=False)

    with pytest.raises(
        LlmCredentialUnavailableError,
        match="lost credential authority",
    ):
        await mark_provider_egress(
            _context(pool=_ReconcilePool(conn)),
            attempt_id=attempt_id,
        )

    assert not any("UPDATE llm_provider_attempts" in query for query in conn.queries)


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

    async def fetchval(self, query: str, *args: Any):
        assert "SELECT state = 'active'" in query
        assert args == ("projectA",)
        return True

    async def fetchrow(self, query: str, *args: Any):
        assert "lease_owner_id = $3" in query
        assert args == ("run1", "projectA", "worker-stale")
        return None


@pytest.mark.asyncio
async def test_stale_supervisor_owner_cannot_begin_a_logical_call():
    context = LlmRequestContext(
        pool=_ReconcilePool(_InactiveOwnerConn()),
        llm_runtime=object(),
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
