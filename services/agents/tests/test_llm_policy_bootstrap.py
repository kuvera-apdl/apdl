"""Operator LLM policy provisioning and immutable audit contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.llm import router
from scripts import provision_llm_policy


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "pipeline/postgres/migrations/037_llm_policy_operator_audit.sql"


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.in_transaction = True
        self.connection.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        del exc, traceback
        self.connection.in_transaction = False
        self.connection.transaction_exit_type = exc_type
        return False


class FakeConnection:
    def __init__(self, *, execution_authorized: bool = True) -> None:
        self.project_policy = {
            "required_data_residency": "local",
            "allow_cross_vendor_retry": False,
            "project_daily_cost_limit_usd_micros": 0,
            "run_cost_limit_usd_micros": 0,
            "execution_authorized": execution_authorized,
        }
        self.previous_providers = [
            {
                "provider": "local",
                "model": "gemma4",
                "endpoint_url": "http://legacy.invalid/v1?api_key=legacy-secret",
                "data_residency": "local",
                "allowed_data_classifications": [
                    "public",
                    "internal",
                    "confidential",
                    "restricted",
                ],
                "input_cost_per_million_tokens_usd_micros": 0,
                "output_cost_per_million_tokens_usd_micros": 0,
                "enabled": True,
            }
        ]
        self.calls: list[tuple[str, tuple[object, ...], bool]] = []
        self.in_transaction = False
        self.transaction_entries = 0
        self.transaction_exit_type = None
        self.close_calls = 0

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return "OK"

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return self.project_policy

    async def fetch(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return self.previous_providers

    async def fetchval(self, query: str, *args):
        self.calls.append((query, args, self.in_transaction))
        return UUID("10000000-0000-4000-8000-000000000037")

    async def close(self) -> None:
        self.close_calls += 1


def _args(**overrides):
    values = {
        "project_id": "demo",
        "provider": "openai",
        "data_residency": "ca",
        "allowed_data_classifications": ["public", "internal"],
        "fast_input_cost_per_million_tokens_usd_micros": 150_000,
        "fast_output_cost_per_million_tokens_usd_micros": 600_000,
        "reasoning_input_cost_per_million_tokens_usd_micros": 250_000,
        "reasoning_output_cost_per_million_tokens_usd_micros": 1_000_000,
        "project_daily_cost_limit_usd_micros": 20_000_000,
        "run_cost_limit_usd_micros": 2_000_000,
        "actor": "operator@example.com",
        "reason": "Enable the reviewed production provider policy",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _configure_openai(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-provider-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.test/v1/")
    monkeypatch.setenv("LLM_FAST_PRIMARY", "fast-reviewed-v1")
    monkeypatch.setenv("LLM_REASONING_PRIMARY", "reasoning-reviewed-v2")


def _compact_sql(connection: FakeConnection) -> list[str]:
    return [" ".join(query.split()) for query, _, _ in connection.calls]


def test_runtime_configuration_is_the_router_and_cli_shared_authority(monkeypatch):
    _configure_openai(monkeypatch)

    configuration = router.provider_runtime_configuration("openai")

    assert configuration.endpoint_url == "https://llm.example.test/v1"
    assert configuration.fast_model == "fast-reviewed-v1"
    assert configuration.reasoning_model == "reasoning-reviewed-v2"
    assert router._tier_models("fast") == [
        {
            "provider": "openai",
            "model": "fast-reviewed-v1",
            "endpoint_url": "https://llm.example.test/v1",
        }
    ]
    assert router._tier_models("reasoning") == [
        {
            "provider": "openai",
            "model": "reasoning-reviewed-v2",
            "endpoint_url": "https://llm.example.test/v1",
        }
    ]


@pytest.mark.parametrize("suffix", ["?api_key=secret", "#secret"])
def test_runtime_endpoint_rejects_secret_bearing_url_components(monkeypatch, suffix):
    _configure_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", f"https://llm.example.test/v1{suffix}")

    with pytest.raises(ValueError, match="query or fragment"):
        router.provider_runtime_configuration("openai")


@pytest.mark.asyncio
async def test_missing_provider_credential_fails_before_database_connect(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://operator-test")

    async def unexpected_connect(_dsn: str):
        raise AssertionError("credential validation must happen before PostgreSQL")

    monkeypatch.setattr(provision_llm_policy.asyncpg, "connect", unexpected_connect)

    with pytest.raises(SystemExit, match="OPENAI_API_KEY is required"):
        await provision_llm_policy.provision(_args())


@pytest.mark.asyncio
async def test_missing_postgres_url_fails_before_connect(monkeypatch):
    _configure_openai(monkeypatch)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    async def unexpected_connect(_dsn: str):
        raise AssertionError("missing DSN must fail before PostgreSQL")

    monkeypatch.setattr(provision_llm_policy.asyncpg, "connect", unexpected_connect)

    with pytest.raises(SystemExit, match="POSTGRES_URL is required"):
        await provision_llm_policy.provision(_args())


@pytest.mark.asyncio
async def test_replacement_is_authorized_locked_atomic_and_non_secret(
    monkeypatch,
    capsys,
):
    _configure_openai(monkeypatch)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://operator-test")
    connection = FakeConnection()

    async def connect(dsn: str):
        assert dsn == "postgresql://operator-test"
        return connection

    monkeypatch.setattr(provision_llm_policy.asyncpg, "connect", connect)

    await provision_llm_policy.provision(_args())

    sql = _compact_sql(connection)
    assert [call[1] for call in connection.calls[:2]] == [
        (provision_llm_policy.MAINTENANCE_INHIBITOR_LOCK_ID,),
        (provision_llm_policy.MAINTENANCE_GUARD_LOCK_ID,),
    ]
    assert all("pg_advisory_lock_shared" in query for query in sql[:2])
    assert all(not in_transaction for _, _, in_transaction in connection.calls[:2])
    assert all(in_transaction for _, _, in_transaction in connection.calls[2:])
    assert connection.transaction_entries == 1
    assert connection.transaction_exit_type is None
    assert connection.close_calls == 1

    authorization_index = next(
        index for index, query in enumerate(sql) if "execution_authorized" in query
    )
    update_index = next(
        index
        for index, query in enumerate(sql)
        if "UPDATE llm_project_policies" in query
    )
    delete_index = next(
        index
        for index, query in enumerate(sql)
        if "DELETE FROM llm_project_provider_policies" in query
    )
    insert_indexes = [
        index
        for index, query in enumerate(sql)
        if "INSERT INTO llm_project_provider_policies" in query
    ]
    audit_index = next(
        index
        for index, query in enumerate(sql)
        if "INSERT INTO llm_project_policy_audit" in query
    )
    assert authorization_index < update_index < delete_index < min(insert_indexes)
    assert max(insert_indexes) < audit_index

    inserted_models = {connection.calls[index][1][2] for index in insert_indexes}
    assert inserted_models == {"fast-reviewed-v1", "reasoning-reviewed-v2"}
    assert all(
        connection.calls[index][1][3] == "https://llm.example.test/v1"
        for index in insert_indexes
    )

    audit_args = connection.calls[audit_index][1]
    previous_snapshot = json.loads(str(audit_args[3]))
    next_snapshot = json.loads(str(audit_args[4]))
    serialized_audit = json.dumps([previous_snapshot, next_snapshot], sort_keys=True)
    assert "legacy-secret" not in serialized_audit
    assert "super-secret-provider-key" not in serialized_audit
    assert "endpoint_url" not in serialized_audit
    assert len(previous_snapshot["provider_policies"][0]["endpoint_sha256"]) == 64
    assert next_snapshot["project_policy"] == {
        "allow_cross_vendor_retry": False,
        "project_daily_cost_limit_usd_micros": 20_000_000,
        "required_data_residency": "ca",
        "run_cost_limit_usd_micros": 2_000_000,
    }
    assert all(item["enabled"] is True for item in next_snapshot["provider_policies"])
    assert "super-secret-provider-key" not in repr(connection.calls)

    output = capsys.readouterr().out
    assert output == (
        "Provisioned openai LLM policy for project demo; "
        "audit_id=10000000-0000-4000-8000-000000000037\n"
    )
    assert "llm.example" not in output
    assert "secret" not in output


@pytest.mark.asyncio
async def test_unauthorized_project_fails_before_any_policy_mutation(monkeypatch):
    _configure_openai(monkeypatch)
    monkeypatch.setenv("POSTGRES_URL", "postgresql://operator-test")
    connection = FakeConnection(execution_authorized=False)

    async def connect(_dsn: str):
        return connection

    monkeypatch.setattr(provision_llm_policy.asyncpg, "connect", connect)

    with pytest.raises(SystemExit, match="not authorized for Agents execution"):
        await provision_llm_policy.provision(_args())

    sql = _compact_sql(connection)
    assert not any("UPDATE llm_project_policies" in query for query in sql)
    assert not any(
        "DELETE FROM llm_project_provider_policies" in query for query in sql
    )
    assert not any("INSERT INTO llm_project_policy_audit" in query for query in sql)
    assert connection.transaction_exit_type is SystemExit
    assert connection.close_calls == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"project_id": "not-valid!"}, "project-id must match"),
        ({"actor": "line one\nline two"}, "actor must be a single line"),
        ({"reason": " "}, "reason is required"),
        (
            {"allowed_data_classifications": ["public", "public"]},
            "must not contain duplicates",
        ),
        ({"data_residency": "local"}, "Remote providers cannot claim local"),
        (
            {"run_cost_limit_usd_micros": 20_000_001},
            "cannot exceed the project limit",
        ),
        (
            {"fast_input_cost_per_million_tokens_usd_micros": -1},
            "must be between 0",
        ),
        ({"provider": "unknown"}, "provider must be"),
        ({"data_residency": "moon"}, "data-residency must be"),
        (
            {"allowed_data_classifications": ["public", "secret"]},
            "Unknown data classifications",
        ),
        (
            {"allowed_data_classifications": []},
            "requires at least one value",
        ),
        (
            {
                "fast_input_cost_per_million_tokens_usd_micros": 0,
                "fast_output_cost_per_million_tokens_usd_micros": 0,
            },
            "Each remote model requires",
        ),
    ],
)
def test_policy_inputs_fail_closed(monkeypatch, overrides, message):
    _configure_openai(monkeypatch)

    with pytest.raises(SystemExit, match=message):
        provision_llm_policy.validate_replacement(_args(**overrides))


def test_local_policy_deduplicates_one_exact_model(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_URL", "http://local-llm:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "local-reviewed")
    replacement = provision_llm_policy.validate_replacement(
        _args(
            provider="local",
            data_residency="local",
            fast_input_cost_per_million_tokens_usd_micros=0,
            fast_output_cost_per_million_tokens_usd_micros=0,
            reasoning_input_cost_per_million_tokens_usd_micros=0,
            reasoning_output_cost_per_million_tokens_usd_micros=0,
        )
    )

    assert len(replacement.provider_policies) == 1
    assert replacement.provider_policies[0].model == "local-reviewed"


def test_remote_policy_requires_https_endpoint(monkeypatch):
    _configure_openai(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://llm.example.test/v1")

    with pytest.raises(SystemExit, match="must use HTTPS"):
        provision_llm_policy.validate_replacement(_args())


def test_migration_creates_immutable_non_secret_policy_audit():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE llm_project_policy_audit" in sql
    assert "previous_policy JSONB NOT NULL" in sql
    assert "next_policy JSONB NOT NULL" in sql
    assert "llm_project_policy_snapshot@1" in sql
    assert "BEFORE UPDATE OR DELETE ON llm_project_policy_audit" in sql
    assert "BEFORE TRUNCATE ON llm_project_policy_audit" in sql
    assert "apdl_reject_llm_policy_audit_mutation" in sql
    assert "ON DELETE RESTRICT" in sql


def test_container_and_make_target_ship_only_environment_credential_workflow():
    dockerfile = (ROOT / "services/agents/Dockerfile").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    script = (ROOT / "services/agents/scripts/provision_llm_policy.py").read_text(
        encoding="utf-8"
    )
    target = makefile.split("provision-agents-llm-policy:", 1)[1].split("\n\n", 1)[0]

    assert (
        "COPY scripts/provision_llm_policy.py scripts/provision_llm_policy.py"
        in dockerfile
    )
    assert "$(COMPOSE) run --rm --build --no-deps agents" in target
    assert "python -m scripts.provision_llm_policy $(ARGS)" in target
    for secret_argument in (
        'add_argument("--api-key"',
        'add_argument("--token"',
        'add_argument("--secret"',
        'add_argument("--password"',
        'add_argument("--postgres-url"',
    ):
        assert secret_argument not in script
