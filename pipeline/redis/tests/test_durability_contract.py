"""Cross-process event-stream durability policy contract tests."""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INGESTION_PRODUCER = (
    REPO_ROOT / "services/ingestion/app/streaming/redis_producer.py"
)
CONFIG_OUTBOX = REPO_ROOT / "services/config/app/outbox.py"
WRITER = REPO_ROOT / "pipeline/redis/clickhouse_writer.py"


def _constant(path: Path, name: str):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"Missing {name} in {path}")


def test_both_event_producers_share_one_bounded_admission_contract():
    for name in (
        "EVENT_STREAM_MAX_ENTRIES",
        "EVENT_STREAM_ALERT_ENTRIES",
        "EVENT_STREAM_ALERT_LOG_INTERVAL_SECONDS",
    ):
        assert _constant(INGESTION_PRODUCER, name) == _constant(CONFIG_OUTBOX, name)
        assert _constant(INGESTION_PRODUCER, name) == _constant(WRITER, name)

    assert _constant(INGESTION_PRODUCER, "_BOUNDED_XADD_LUA") == _constant(
        CONFIG_OUTBOX,
        "_BOUNDED_XADD_LUA",
    )

    script = _constant(INGESTION_PRODUCER, "_BOUNDED_XADD_LUA")
    assert "XLEN" in script
    assert "MAXLEN" not in script


def test_supported_redis_configs_persist_and_never_evict_accepted_streams():
    for relative_path in (
        "infra/docker/docker-compose.yml",
        "infra/docker/docker-compose.deps.yml",
    ):
        config = (REPO_ROOT / relative_path).read_text()
        redis_command = next(
            line.strip()
            for line in config.splitlines()
            if line.strip().startswith("command: redis-server")
        )
        assert "--appendonly yes" in redis_command
        assert "--appendfsync everysec" in redis_command
        assert "--maxmemory " in redis_command
        assert "--maxmemory-policy noeviction" in redis_command
        assert "allkeys-lru" not in redis_command
