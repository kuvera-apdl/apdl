"""Static contracts for the Admin console's trusted proxy boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _nginx_location(config: str, declaration: str) -> str:
    return config.split(declaration, 1)[1].split("\n    }", 1)[0]


def test_admin_nginx_overwrites_untrusted_forwarding_headers() -> None:
    config = (ROOT / "services/admin/nginx.conf").read_text(encoding="utf-8")
    auth_location = _nginx_location(
        config,
        "location ~ ^/api/auth/(login|register)$ {",
    )
    api_location = _nginx_location(config, "location /api/ {")

    assert "$proxy_add_x_forwarded_for" not in config
    for location in (auth_location, api_location):
        assert 'proxy_set_header Forwarded "";' in location
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in location
        assert 'proxy_set_header X-Real-IP "";' in location


def test_admin_nginx_uses_global_and_ip_abuse_limits_with_canonical_429() -> None:
    config = (ROOT / "services/admin/nginx.conf").read_text(encoding="utf-8")
    auth_location = _nginx_location(
        config,
        "location ~ ^/api/auth/(login|register)$ {",
    )
    throttled_location = _nginx_location(config, "location @auth_throttled {")

    assert "zone=admin_auth_ip:" in config
    assert "zone=admin_auth_global:" in config
    assert "limit_req zone=admin_auth_ip" in auth_location
    assert "limit_req zone=admin_auth_global" in auth_location
    assert "error_page 429 = @auth_throttled;" in auth_location
    assert 'add_header Retry-After "60" always;' in throttled_location
    assert (
        """{"error":"auth_throttled","message":"Too many attempts. """
        """Try again later.","retry_after_seconds":60}"""
        in throttled_location
    )


def test_admin_uvicorn_preserves_socket_peer_for_application_policy() -> None:
    dockerfile = (ROOT / "services/admin-api/Dockerfile").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    run_admin_api = makefile.split("run-admin-api:", 1)[1].split("\n\n", 1)[0]

    assert '"--no-proxy-headers"' in dockerfile
    assert "--forwarded-allow-ips" not in dockerfile
    assert "--no-proxy-headers" in run_admin_api


def test_compose_limits_forwarding_trust_to_the_admin_edge_network() -> None:
    compose = (ROOT / "infra/docker/docker-compose.yml").read_text(encoding="utf-8")
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert (
        "APDL_ADMIN_TRUSTED_PROXY_CIDRS: "
        """'${APDL_ADMIN_TRUSTED_PROXY_CIDRS:-["172.30.255.0/28"]}'"""
        in compose
    )
    assert "admin-edge:" in compose
    assert "subnet: 172.30.255.0/28" in compose
    assert 'APDL_ADMIN_TRUSTED_PROXY_CIDRS=["172.30.255.0/28"]' in environment


def test_compose_fails_closed_while_local_example_enables_bounded_onboarding() -> None:
    compose = (ROOT / "infra/docker/docker-compose.yml").read_text(encoding="utf-8")
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert (
        "APDL_ADMIN_REGISTRATION_ENABLED: "
        "${APDL_ADMIN_REGISTRATION_ENABLED:-false}"
    ) in compose
    assert "APDL_ADMIN_MAX_ACCOUNTS: ${APDL_ADMIN_MAX_ACCOUNTS:-100}" in compose
    assert (
        "APDL_ADMIN_MAX_PROJECTS_PER_USER: "
        "${APDL_ADMIN_MAX_PROJECTS_PER_USER:-5}"
    ) in compose
    assert "APDL_ADMIN_REGISTRATION_ENABLED=true" in environment
    assert "APDL_ADMIN_MAX_ACCOUNTS=100" in environment
    assert "APDL_ADMIN_MAX_PROJECTS_PER_USER=5" in environment
    assert "APDL_DEV_API_KEY=" not in environment
    assert "APDL_DEV_CLIENT_KEY=" not in environment


def test_local_example_keeps_shared_routing_complete_and_last() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "infra/docker/docker-compose.yml").read_text(encoding="utf-8")
    routing = environment.split("# ── Routing", maxsplit=1)[1]

    for assignment in (
        "REDIS_URL=redis://localhost:6379",
        "POSTGRES_URL=postgresql://apdl_runtime:apdl_runtime_dev"
        "@localhost:5432/apdl",
        "CLICKHOUSE_URL=http://localhost:8123",
        "OPENAI_BASE_URL=https://api.openai.com/v1",
        "ANTHROPIC_BASE_URL=https://api.anthropic.com",
        "INGESTION_SERVICE_URL=http://localhost:8080",
        "CONFIG_SERVICE_URL=http://localhost:8081",
        "QUERY_SERVICE_URL=http://localhost:8082",
        "AGENTS_SERVICE_URL=http://localhost:8083",
        "CODEGEN_SERVICE_URL=http://localhost:8084",
        "ADMIN_API_URL=http://localhost:8085",
    ):
        assert assignment in routing

    assert environment.index("# ── Common") < environment.index("# ── Admin API")
    assert environment.index("# ── Admin API") < environment.index(
        "# ── Config Service"
    )
    assert environment.index("# ── Config Service") < environment.index(
        "# ── Agents Service"
    )
    assert environment.index("# ── Agents Service") < environment.index(
        "# ── Codegen Service"
    )
    assert environment.index("# ── Codegen Service") < environment.index(
        "# ── ClickHouse Writer"
    )
    assert environment.index("# ── ClickHouse Writer") < environment.index(
        "# ── Routing"
    )
    assert "ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-}" in compose
    assert "OPENAI_BASE_URL: ${OPENAI_BASE_URL:-}" in compose
