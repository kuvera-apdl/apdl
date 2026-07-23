"""Static security contracts for the local dependency stack."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
DEPS_COMPOSE = ROOT / "infra" / "docker" / "docker-compose.deps.yml"


class ComposeSecurityTests(unittest.TestCase):
    def test_dependency_ports_bind_to_loopback_by_default(self) -> None:
        compose = DEPS_COMPOSE.read_text(encoding="utf-8")

        for mapping in (
            "${APDL_BIND_ADDRESS:-127.0.0.1}:${APDL_REDIS_HOST_PORT:-6379}:6379",
            "${APDL_BIND_ADDRESS:-127.0.0.1}:${APDL_CLICKHOUSE_HTTP_HOST_PORT:-8123}:8123",
            "${APDL_BIND_ADDRESS:-127.0.0.1}:${APDL_CLICKHOUSE_NATIVE_HOST_PORT:-9000}:9000",
            "${APDL_BIND_ADDRESS:-127.0.0.1}:${APDL_POSTGRES_HOST_PORT:-5432}:5432",
        ):
            self.assertIn(f'"{mapping}"', compose)

    def test_dependency_stack_has_no_bare_host_port_mappings(self) -> None:
        compose = DEPS_COMPOSE.read_text(encoding="utf-8")

        self.assertIsNone(
            re.search(r'^\s*-\s*["\']?\d+:\d+', compose, re.MULTILINE)
        )


if __name__ == "__main__":
    unittest.main()
