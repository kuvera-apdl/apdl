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

    if grep -qiE \
        '(^|[^A-Za-z0-9_])(events_v2|decisions_v2|feeds_v2)([^A-Za-z0-9_]|$)' \
        "$migration"; then
        echo "Unsupported ETL v2 schema in release migration: $migration" >&2
        exit 1
    fi

    echo "  Applying $(basename "$migration")"
    docker exec -i "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DB" \
        --multiquery < "$migration"
done

backfill_lock_dir="${TMPDIR:-/tmp}/apdl-clickhouse-backfills-$container_id.lock"
backfill_snapshot=""

cleanup_backfill() {
    if [ -n "$backfill_snapshot" ]; then
        rm -f "$backfill_snapshot"
    fi
    rmdir "$backfill_lock_dir" 2>/dev/null || true
}

if ! mkdir "$backfill_lock_dir" 2>/dev/null; then
    echo "Another ClickHouse backfill runner holds: $backfill_lock_dir" >&2
    exit 1
fi
trap cleanup_backfill EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker exec "$container_id" clickhouse-client \
    --user "$CLICKHOUSE_USER" \
    --password "$CLICKHOUSE_PASSWORD" \
    --database "$CLICKHOUSE_DB" \
    --query "
        CREATE TABLE IF NOT EXISTS apdl_schema_backfills (
            name String,
            checksum FixedString(64),
            completed_at DateTime64(3)
        ) ENGINE = ReplacingMergeTree(completed_at)
        ORDER BY (name, checksum)
    " >/dev/null

for backfill in "$CLICKHOUSE_BACKFILLS_DIR"/*.sql; do
    [ -f "$backfill" ] || continue

    backfill_name="$(basename "$backfill")"
    if [[ ! "$backfill_name" =~ ^[0-9]{3}_[a-z0-9_]+\.sql$ ]]; then
        echo "Invalid ClickHouse backfill name: $backfill_name" >&2
        exit 1
    fi

    backfill_snapshot="$(mktemp "${TMPDIR:-/tmp}/apdl-clickhouse-backfill.XXXXXX.sql")"
    cp "$backfill" "$backfill_snapshot"
    if command -v sha256sum >/dev/null 2>&1; then
        backfill_checksum="$(sha256sum "$backfill_snapshot" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        backfill_checksum="$(shasum -a 256 "$backfill_snapshot" | awk '{print $1}')"
    else
        echo "A SHA-256 utility is required for ClickHouse backfills" >&2
        exit 1
    fi

    recorded_checksum="$(docker exec "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DB" \
        --format TSVRaw \
        --query "
            SELECT multiIf(
                count() = 0,
                '',
                uniqExact(checksum) = 1,
                toString(any(checksum)),
                '__multiple_checksums__'
            )
            FROM apdl_schema_backfills FINAL
            WHERE name = '$backfill_name'
        ")"

    if [ -n "$recorded_checksum" ]; then
        if [ "$recorded_checksum" != "$backfill_checksum" ]; then
            echo "ClickHouse backfill checksum drift: $backfill_name" >&2
            exit 1
        fi
        echo "  Already applied $backfill_name"
        rm -f "$backfill_snapshot"
        backfill_snapshot=""
        continue
    fi

    echo "  Backfilling $backfill_name"
    docker exec -i "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DB" \
        --multiquery < "$backfill_snapshot"
    docker exec "$container_id" clickhouse-client \
        --user "$CLICKHOUSE_USER" \
        --password "$CLICKHOUSE_PASSWORD" \
        --database "$CLICKHOUSE_DB" \
        --query "
            INSERT INTO apdl_schema_backfills
                (name, checksum, completed_at)
            VALUES ('$backfill_name', '$backfill_checksum', now64(3))
        " >/dev/null
    rm -f "$backfill_snapshot"
    backfill_snapshot=""
done

trap - EXIT INT TERM
cleanup_backfill

echo "==> ClickHouse initialization complete"
