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
CLICKHOUSE_BACKFILLS_DIR="${CLICKHOUSE_BACKFILLS_DIR:-$ROOT_DIR/pipeline/clickhouse/backfills}"
CLICKHOUSE_READY_RETRIES="${CLICKHOUSE_READY_RETRIES:-30}"
CLICKHOUSE_READY_INTERVAL="${CLICKHOUSE_READY_INTERVAL:-2}"
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-apdl}"
POSTGRES_DB="${POSTGRES_DB:-apdl}"

if [[ ! "$CLICKHOUSE_DB" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Invalid ClickHouse database name: $CLICKHOUSE_DB" >&2
    exit 1
fi

if [ ! -d "$CLICKHOUSE_MIGRATIONS_DIR" ]; then
    echo "ClickHouse migrations directory not found: $CLICKHOUSE_MIGRATIONS_DIR" >&2
    exit 1
fi

if [ ! -d "$CLICKHOUSE_BACKFILLS_DIR" ]; then
    echo "ClickHouse backfills directory not found: $CLICKHOUSE_BACKFILLS_DIR" >&2
    exit 1
fi

echo "==> Initializing ClickHouse"
docker compose "${COMPOSE_ARGS[@]}" up -d \
    "$CLICKHOUSE_SERVICE" "$POSTGRES_SERVICE" >/dev/null

container_id="$(docker compose "${COMPOSE_ARGS[@]}" ps -q "$CLICKHOUSE_SERVICE")"
if [ -z "$container_id" ]; then
    echo "ClickHouse container is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi

ready=0
for _ in $(seq 1 "$CLICKHOUSE_READY_RETRIES"); do
    # The official image starts a temporary child server while its PID 1
    # entrypoint creates CLICKHOUSE_DB, then stops that child and execs the
    # durable server. A successful query against the temporary server is not a
    # startup guarantee: migrations launched in that window lose their client
    # during the handoff. Require PID 1 to be the final ClickHouse executable
    # before accepting the readiness query.
    if docker exec "$container_id" sh -c '
        command="$(tr "\000" "\n" </proc/1/cmdline | head -n 1)" || exit 1
        case "$command" in
            clickhouse-server|*/clickhouse-server) exit 0 ;;
            *) exit 1 ;;
        esac
    ' >/dev/null 2>&1 \
        && docker exec "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --query "SELECT 1" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep "$CLICKHOUSE_READY_INTERVAL"
done

if [ "$ready" -ne 1 ]; then
    echo "ClickHouse final server process did not become ready in time." >&2
    exit 1
fi

postgres_container_id="$(
    docker compose "${COMPOSE_ARGS[@]}" ps -q "$POSTGRES_SERVICE"
)"
if [ -z "$postgres_container_id" ]; then
    echo "PostgreSQL container is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi

postgres_ready=0
for _ in $(seq 1 "$CLICKHOUSE_READY_RETRIES"); do
    if docker exec "$postgres_container_id" sh -c '
        command="$(tr "\000" "\n" </proc/1/cmdline | head -n 1)" || exit 1
        case "$command" in
            postgres|*/postgres) exit 0 ;;
            *) exit 1 ;;
        esac
    ' >/dev/null 2>&1 \
        && docker exec "$postgres_container_id" pg_isready \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
        postgres_ready=1
        break
    fi
    sleep "$CLICKHOUSE_READY_INTERVAL"
done
if [ "$postgres_ready" -ne 1 ]; then
    echo "PostgreSQL final maintenance coordinator did not become ready in time." >&2
    exit 1
fi

PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT_DIR/scripts/migration_quiescence.py" \
    --anchor-container "$container_id" \
    --service clickhouse-writer

CLICKHOUSE_CONTAINER_ID="$container_id" \
CLICKHOUSE_USER="$CLICKHOUSE_USER" \
CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" \
CLICKHOUSE_DB="$CLICKHOUSE_DB" \
CLICKHOUSE_MIGRATIONS_DIR="$CLICKHOUSE_MIGRATIONS_DIR" \
CLICKHOUSE_BACKFILLS_DIR="$CLICKHOUSE_BACKFILLS_DIR" \
POSTGRES_CONTAINER_ID="$postgres_container_id" \
POSTGRES_USER="$POSTGRES_USER" \
POSTGRES_DB="$POSTGRES_DB" \
PYTHONDONTWRITEBYTECODE=1 \
    python3 "$ROOT_DIR/pipeline/clickhouse/migrate.py"

echo "==> ClickHouse initialization complete"
