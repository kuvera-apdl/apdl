from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest

from app import service_auth
from app.service_auth import (
    CAPABILITY_HEADER,
    CAPABILITY_TTL_SECONDS,
    ServiceCapabilityUnavailableError,
    service_headers,
)
from tests.capability_helpers import make_service_capability


class _Transaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, *, active: bool = True):
        self.active = active
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self):
        return _Transaction()

    async def fetchval(self, query: str, *args: Any):
        self.fetches.append((query, args))
        return self.active

    async def execute(self, query: str, *args: Any):
        self.executes.append((query, args))
        return "OK"


class _Acquire(AbstractAsyncContextManager):
    def __init__(self, connection: _Connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, *, active: bool = True):
        self.connection = _Connection(active=active)

    def acquire(self):
        return _Acquire(self.connection)


def _effect_context(*, pool: Any):
    return make_service_capability(
        project_id="demo",
        execution_kind="approval_effect",
        execution_id="4b42b08a-52fe-4d73-8ed8-a5d9a67a2f21",
        run_id="run-7",
        execution_owner_id="effect-owner-7",
        pool=pool,
    )


@pytest.mark.asyncio
async def test_service_headers_issues_hash_only_capability_and_revokes_on_exit(
    monkeypatch,
):
    pool = _Pool()
    context = make_service_capability(pool=pool)
    monkeypatch.setattr(service_auth.secrets, "token_urlsafe", lambda _size: "A" * 43)
    raw_token = f"apdlcap_{'A' * 43}"
    token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()

    async with service_headers(
        context,
        audiences=("config", "query"),
        roles=("query:read",),
    ) as headers:
        assert headers == {CAPABILITY_HEADER: raw_token}
        assert "X-API-Key" not in headers
        assert len(pool.connection.executes) == 2

    assert len(pool.connection.fetches) == 1
    assert pool.connection.fetches[0][1] == (
        "run-1",
        "demo",
        "lease-owner-1",
    )
    assert "agent_runs" in pool.connection.fetches[0][0]

    cleanup, insert, revocation = pool.connection.executes
    assert "expires_at <= now()" in cleanup[0]
    assert cleanup[1] == ()
    assert "INSERT INTO agent_service_capabilities" in insert[0]
    assert insert[1] == (
        token_hash,
        "demo",
        "agent_run",
        "run-1",
        "run-1",
        "lease-owner-1",
        ["config", "query"],
        ["query:read"],
        None,
        CAPABILITY_TTL_SECONDS,
    )
    assert raw_token not in repr(insert[1])
    assert "WHERE token_hash = $1" in revocation[0]
    assert revocation[1] == (token_hash,)


@pytest.mark.asyncio
async def test_service_headers_revokes_when_the_call_body_raises(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(service_auth.secrets, "token_urlsafe", lambda _size: "B" * 43)

    with pytest.raises(RuntimeError, match="transport failed"):
        async with service_headers(
            make_service_capability(pool=pool),
            audiences=("query",),
            roles=("query:read",),
        ):
            raise RuntimeError("transport failed")

    assert len(pool.connection.executes) == 3
    assert "WHERE token_hash = $1" in pool.connection.executes[-1][0]


@pytest.mark.asyncio
async def test_normal_run_can_receive_config_agents_read_authority():
    pool = _Pool()

    async with service_headers(
        make_service_capability(pool=pool),
        audiences=("config",),
        roles=("agents:read",),
    ):
        pass

    insert = pool.connection.executes[1]
    assert insert[1][6:8] == (["config"], ["agents:read"])


@pytest.mark.asyncio
async def test_service_headers_refuses_inactive_execution_without_issuing():
    pool = _Pool(active=False)

    with pytest.raises(
        ServiceCapabilityUnavailableError,
        match="no longer owns internal service authority",
    ):
        async with service_headers(
            make_service_capability(pool=pool),
            audiences=("query",),
            roles=("query:read",),
        ):
            pytest.fail("inactive execution received a capability")

    assert pool.connection.executes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_kind", "execution_id", "run_id", "owner", "expected_table", "args"),
    [
        (
            "agent_run",
            "run-8",
            "run-8",
            "run-owner-8",
            "agent_runs",
            ("run-8", "demo", "run-owner-8"),
        ),
        (
            "custom_agent_test",
            "test-8",
            "test-parent-8",
            "test-owner-8",
            "custom_agent_test_runs",
            ("test-8", "demo"),
        ),
        (
            "approval_effect",
            "4b42b08a-52fe-4d73-8ed8-a5d9a67a2f21",
            "run-8",
            "effect-owner-8",
            "agent_approval_effects",
            (
                "4b42b08a-52fe-4d73-8ed8-a5d9a67a2f21",
                "run-8",
                "demo",
                "effect-owner-8",
            ),
        ),
    ],
)
async def test_service_headers_checks_exact_durable_execution_lease(
    execution_kind,
    execution_id,
    run_id,
    owner,
    expected_table,
    args,
):
    pool = _Pool()
    context = make_service_capability(
        project_id="demo",
        execution_kind=execution_kind,
        execution_id=execution_id,
        run_id=run_id,
        execution_owner_id=owner,
        pool=pool,
    )

    async with service_headers(
        context,
        audiences=("query",),
        roles=("query:read",),
    ):
        pass

    assert expected_table in pool.connection.fetches[0][0]
    assert pool.connection.fetches[0][1] == args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audiences", "roles"),
    [
        (("config",), ("config:write",)),
        (("codegen",), ("agents:manage",)),
    ],
)
@pytest.mark.parametrize("execution_kind", ["agent_run", "custom_agent_test"])
async def test_read_executions_cannot_receive_mutation_authority(
    execution_kind,
    audiences,
    roles,
):
    pool = _Pool()
    context = make_service_capability(
        execution_kind=execution_kind,
        execution_id="run-1" if execution_kind == "agent_run" else "test-1",
        pool=pool,
    )

    with pytest.raises(
        ValueError,
        match="Only a leased approval effect may receive mutation authority",
    ):
        async with service_headers(
            context,
            audiences=audiences,
            roles=roles,
        ):
            pytest.fail("read execution received mutation authority")

    assert pool.connection.fetches == []
    assert pool.connection.executes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audiences", "roles"),
    [
        (("config",), ("config:write",)),
        (("codegen",), ("agents:manage",)),
    ],
)
async def test_leased_approval_effect_can_receive_mutation_authority(
    audiences,
    roles,
):
    pool = _Pool()
    path = "/v1/admin/experiments" if audiences == ("config",) else "/v1/changesets"
    payload = {"project_id": "demo", "key": "approved-item"}

    async with service_headers(
        _effect_context(pool=pool),
        audiences=audiences,
        roles=roles,
        request_method="POST",
        request_path=path,
        request_json=payload,
        idempotency_key="effect:1",
    ) as headers:
        assert set(headers) == {CAPABILITY_HEADER}

    assert len(pool.connection.fetches) == 1
    assert len(pool.connection.executes) == 3
    insert_args = pool.connection.executes[1][1]
    assert insert_args[8] == service_auth.canonical_request_sha256(
        method="POST",
        path=path,
        json_body=payload,
        idempotency_key="effect:1",
    )


@pytest.mark.asyncio
async def test_mutation_authority_requires_exact_request_binding():
    pool = _Pool()

    with pytest.raises(ValueError, match="exact canonical request binding"):
        async with service_headers(
            _effect_context(pool=pool),
            audiences=("config",),
            roles=("config:write",),
        ):
            pytest.fail("unbound mutation authority was issued")

    assert pool.connection.fetches == []
    assert pool.connection.executes == []


def test_canonical_request_hash_binds_method_path_body_and_idempotency_key():
    baseline = service_auth.canonical_request_sha256(
        method="POST",
        path="/v1/admin/experiments",
        json_body={"z": 1, "nested": {"b": 2, "a": "\N{SNOWMAN}"}},
        idempotency_key="effect:1",
    )
    reordered = service_auth.canonical_request_sha256(
        method="POST",
        path="/v1/admin/experiments",
        json_body={"nested": {"a": "\N{SNOWMAN}", "b": 2}, "z": 1},
        idempotency_key="effect:1",
    )

    assert baseline == reordered
    assert baseline != service_auth.canonical_request_sha256(
        method="PUT",
        path="/v1/admin/experiments",
        json_body={"nested": {"a": "\N{SNOWMAN}", "b": 2}, "z": 1},
        idempotency_key="effect:1",
    )
    assert baseline != service_auth.canonical_request_sha256(
        method="POST",
        path="/v1/admin/experiments/other",
        json_body={"nested": {"a": "\N{SNOWMAN}", "b": 2}, "z": 1},
        idempotency_key="effect:1",
    )
    assert baseline != service_auth.canonical_request_sha256(
        method="POST",
        path="/v1/admin/experiments",
        json_body={"nested": {"a": "\N{SNOWMAN}", "b": 2}, "z": 2},
        idempotency_key="effect:1",
    )
    assert baseline != service_auth.canonical_request_sha256(
        method="POST",
        path="/v1/admin/experiments",
        json_body={"nested": {"a": "\N{SNOWMAN}", "b": 2}, "z": 1},
        idempotency_key="effect:2",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audiences", "roles", "label"),
    [
        ((), ("query:read",), "audiences"),
        (("query", "config"), ("query:read",), "audiences"),
        (("query", "query"), ("query:read",), "audiences"),
        (("query",), (), "roles"),
        (("query",), ("agents:read", "query:read"), "roles"),
        (("query",), ("query:read", "query:read"), "roles"),
    ],
)
async def test_service_headers_rejects_noncanonical_authority(
    audiences,
    roles,
    label,
):
    pool = _Pool()

    with pytest.raises(ValueError, match=label):
        async with service_headers(
            make_service_capability(pool=pool),
            audiences=audiences,
            roles=roles,
        ):
            pytest.fail("noncanonical authority was accepted")

    assert pool.connection.fetches == []
    assert pool.connection.executes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audiences", "roles"),
    [
        (("config",), ("agents:manage",)),
        (("query",), ("agents:read",)),
        (("codegen",), ("query:read",)),
        (("config", "query"), ("agents:read",)),
    ],
)
async def test_service_headers_rejects_canonical_but_unsupported_authority_shape(
    audiences,
    roles,
):
    pool = _Pool()

    with pytest.raises(ValueError, match="allowed authority shape"):
        async with service_headers(
            make_service_capability(pool=pool),
            audiences=audiences,
            roles=roles,
        ):
            pytest.fail("unsupported authority shape was accepted")

    assert pool.connection.fetches == []
    assert pool.connection.executes == []
