#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker/docker-compose.yml"
PROJECT_NAME="apdl-query-selectors-$$"

compose() {
    APDL_BIND_ADDRESS=127.0.0.1 \
    APDL_CLICKHOUSE_HTTP_HOST_PORT=0 \
    APDL_CLICKHOUSE_NATIVE_HOST_PORT=0 \
        docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
}

cleanup() {
    compose down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

pinned_image="$(
    compose config --format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["clickhouse"]["image"])'
)"
if [[ ! "$pinned_image" =~ ^clickhouse/clickhouse-server:[^@]+@sha256:[0-9a-f]{64}$ ]]; then
    echo "ClickHouse runtime must be pinned by tag and digest: $pinned_image" >&2
    exit 1
fi

echo "==> Starting exact shipped ClickHouse image: $pinned_image"
compose up -d --wait --wait-timeout 90 clickhouse >/dev/null
native_endpoint="$(compose port clickhouse 9000)"
native_port="${native_endpoint##*:}"
engine_version="$(
    compose exec -T clickhouse clickhouse-client \
        --user apdl \
        --password apdl_dev \
        --database apdl \
        --format TSVRaw \
        --query 'SELECT version()'
)"
echo "==> Executing selector matrix on ClickHouse $engine_version"
(
    cd "$ROOT_DIR/services/query"
    APDL_TEST_CLICKHOUSE_HOST=127.0.0.1 \
    APDL_TEST_CLICKHOUSE_PORT="$native_port" \
    APDL_TEST_CLICKHOUSE_USER=apdl \
    APDL_TEST_CLICKHOUSE_PASSWORD=apdl_dev \
    APDL_TEST_CLICKHOUSE_DB=apdl \
        .venv/bin/python -m pytest -q tests/test_selector_clickhouse.py
)
