#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

COMPOSE_FILE="${CLICKHOUSE_COMPOSE_FILE:-$ROOT_DIR/infra/docker/docker-compose.deps.yml}"
if [[ "$COMPOSE_FILE" != /* ]]; then
    COMPOSE_FILE="$ROOT_DIR/$COMPOSE_FILE"
fi

# Load the repo-root .env so ${...} interpolation in the (full) compose file
# resolves; the file is parsed even though this script only targets ClickHouse.
COMPOSE_ARGS=(-f "$COMPOSE_FILE")
[ -f "$ROOT_DIR/.env" ] && COMPOSE_ARGS=(--env-file "$ROOT_DIR/.env" "${COMPOSE_ARGS[@]}")

CLICKHOUSE_SERVICE="${CLICKHOUSE_SERVICE:-clickhouse}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-apdl}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-apdl_dev}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-apdl}"
CLICKHOUSE_MIGRATIONS_DIR="${CLICKHOUSE_MIGRATIONS_DIR:-$ROOT_DIR/pipeline/clickhouse/migrations}"
CLICKHOUSE_READY_RETRIES="${CLICKHOUSE_READY_RETRIES:-30}"
CLICKHOUSE_READY_INTERVAL="${CLICKHOUSE_READY_INTERVAL:-2}"

if [[ ! "$CLICKHOUSE_DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Invalid ClickHouse database name: $CLICKHOUSE_DB" >&2
    exit 1
fi

if [ ! -d "$CLICKHOUSE_MIGRATIONS_DIR" ]; then
    echo "ClickHouse migrations directory not found: $CLICKHOUSE_MIGRATIONS_DIR" >&2
    exit 1
fi

echo "==> Initializing ClickHouse"
docker compose "${COMPOSE_ARGS[@]}" up -d "$CLICKHOUSE_SERVICE" >/dev/null

container_id="$(docker compose "${COMPOSE_ARGS[@]}" ps -q "$CLICKHOUSE_SERVICE")"
if [ -z "$container_id" ]; then
    echo "ClickHouse container is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi

ready=0
for _ in $(seq 1 "$CLICKHOUSE_READY_RETRIES"); do
    if docker exec "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --query "SELECT 1" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep "$CLICKHOUSE_READY_INTERVAL"
done

if [ "$ready" -ne 1 ]; then
    echo "ClickHouse did not become ready in time." >&2
    exit 1
fi

docker exec "$container_id" clickhouse-client \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --query "CREATE DATABASE IF NOT EXISTS \`$CLICKHOUSE_DB\`" >/dev/null

for migration in "$CLICKHOUSE_MIGRATIONS_DIR"/*.sql; do
    [ -f "$migration" ] || continue

    if grep -qiE "NOT ClickHouse|Target:[[:space:]]*PostgreSQL|psql[[:space:]]+\\\$POSTGRES_URL" "$migration"; then
        echo "Misplaced PostgreSQL migration in ClickHouse directory: $migration" >&2
        exit 1
    fi

    echo "  Applying $(basename "$migration")"
    docker exec -i "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DB" \
        --multiquery < "$migration"
done

echo "==> ClickHouse initialization complete"
