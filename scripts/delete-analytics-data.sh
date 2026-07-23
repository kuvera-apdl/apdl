#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${CLICKHOUSE_COMPOSE_FILE:-$ROOT_DIR/infra/docker/docker-compose.deps.yml}"
if [[ "$COMPOSE_FILE" != /* ]]; then
    COMPOSE_FILE="$ROOT_DIR/$COMPOSE_FILE"
fi

COMPOSE_ARGS=(-f "$COMPOSE_FILE")
[ -f "$ROOT_DIR/.env" ] \
    && COMPOSE_ARGS=(--env-file "$ROOT_DIR/.env" "${COMPOSE_ARGS[@]}")

CLICKHOUSE_SERVICE="${CLICKHOUSE_SERVICE:-clickhouse}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
clickhouse_container_id="$(
    docker compose "${COMPOSE_ARGS[@]}" ps -q "$CLICKHOUSE_SERVICE"
)"
postgres_container_id="$(
    docker compose "${COMPOSE_ARGS[@]}" ps -q "$POSTGRES_SERVICE"
)"

if [ -z "$clickhouse_container_id" ]; then
    echo "ClickHouse is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi
if [ -z "$postgres_container_id" ]; then
    echo "PostgreSQL is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi

export CLICKHOUSE_CONTAINER_ID="$clickhouse_container_id"
export POSTGRES_CONTAINER_ID="$postgres_container_id"
export CLICKHOUSE_USER="${CLICKHOUSE_USER:-apdl}"
export CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-apdl_dev}"
export CLICKHOUSE_DB="${CLICKHOUSE_DB:-apdl}"
export POSTGRES_USER="${POSTGRES_USER:-apdl}"
export POSTGRES_DB="${POSTGRES_DB:-apdl}"

exec python3 "$ROOT_DIR/pipeline/clickhouse/delete_analytics_data.py" "$@"
