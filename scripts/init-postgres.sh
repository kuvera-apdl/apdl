#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${POSTGRES_COMPOSE_FILE:-$ROOT_DIR/infra/docker/docker-compose.deps.yml}"
if [[ "$COMPOSE_FILE" != /* ]]; then
    COMPOSE_FILE="$ROOT_DIR/$COMPOSE_FILE"
fi

COMPOSE_ARGS=(-f "$COMPOSE_FILE")
if [ -n "${POSTGRES_COMPOSE_OVERRIDE_FILE:-}" ]; then
    COMPOSE_OVERRIDE_FILE="$POSTGRES_COMPOSE_OVERRIDE_FILE"
    if [[ "$COMPOSE_OVERRIDE_FILE" != /* ]]; then
        COMPOSE_OVERRIDE_FILE="$ROOT_DIR/$COMPOSE_OVERRIDE_FILE"
    fi
    [ -f "$COMPOSE_OVERRIDE_FILE" ] || {
        echo "PostgreSQL Compose override not found: $COMPOSE_OVERRIDE_FILE" >&2
        exit 1
    }
    COMPOSE_ARGS+=(-f "$COMPOSE_OVERRIDE_FILE")
fi
[ -f "$ROOT_DIR/.env" ] && COMPOSE_ARGS=(--env-file "$ROOT_DIR/.env" "${COMPOSE_ARGS[@]}")

env_file_value() {
    local key="$1"
    [ -f "$ROOT_DIR/.env" ] || return 0
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ROOT_DIR/.env"
}

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_MIGRATOR_SERVICE="${POSTGRES_MIGRATOR_SERVICE:-postgres-migrate}"
POSTGRES_USER="${POSTGRES_USER:-apdl}"
POSTGRES_DB="${POSTGRES_DB:-apdl}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(env_file_value POSTGRES_PASSWORD)}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-apdl_dev}"
MIGRATIONS_DIR="${POSTGRES_MIGRATIONS_DIR:-$ROOT_DIR/pipeline/postgres/migrations}"
POSTGRES_MIGRATOR_BUILD="${POSTGRES_MIGRATOR_BUILD:-true}"
POSTGRES_USE_PACKAGED_MIGRATIONS="${POSTGRES_USE_PACKAGED_MIGRATIONS:-false}"

echo "==> Initializing PostgreSQL"
docker compose "${COMPOSE_ARGS[@]}" up -d "$POSTGRES_SERVICE" >/dev/null
container_id="$(docker compose "${COMPOSE_ARGS[@]}" ps -q "$POSTGRES_SERVICE")"
if [ -z "$container_id" ]; then
    echo "PostgreSQL container is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi

ready=0
for _ in $(seq 1 30); do
    # The official image runs initdb against a temporary child server before
    # PID 1 execs the durable PostgreSQL server. Do not let a transient
    # pg_isready success start a fence owner or migration that the handoff will
    # immediately destroy.
    if docker exec "$container_id" sh -c '
        command="$(tr "\000" "\n" </proc/1/cmdline | head -n 1)" || exit 1
        case "$command" in
            postgres|*/postgres) exit 0 ;;
            *) exit 1 ;;
        esac
    ' >/dev/null 2>&1 \
        && docker exec "$container_id" pg_isready \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || {
    echo "PostgreSQL final server process did not become ready in time." >&2
    exit 1
}

# Resolve all image/build work before the final local drain check. Release
# verification supplies and pre-pulls an immutable migrator image, while local
# development retains the source-build default.
case "$POSTGRES_MIGRATOR_BUILD" in
    true)
        docker compose "${COMPOSE_ARGS[@]}" build "$POSTGRES_MIGRATOR_SERVICE" >/dev/null
        ;;
    false)
        ;;
    *)
        echo "POSTGRES_MIGRATOR_BUILD must be true or false" >&2
        exit 1
        ;;
esac

quiescence_args=(
    --anchor-container "$container_id"
    --service ingestion
    --service config
    --service query
    --service agents
    --service codegen
    --service clickhouse-writer
    --service admin-api
    --service admin
    --service gateway
)
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT_DIR/scripts/migration_quiescence.py" \
    "${quiescence_args[@]}"

migration_mount_args=()
case "$POSTGRES_USE_PACKAGED_MIGRATIONS" in
    true)
        ;;
    false)
        migration_mount_args=(-v "$MIGRATIONS_DIR:/migrations:ro")
        ;;
    *)
        echo "POSTGRES_USE_PACKAGED_MIGRATIONS must be true or false" >&2
        exit 1
        ;;
esac

docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps \
    -e PGHOST="$POSTGRES_SERVICE" \
    -e PGPORT=5432 \
    -e PGUSER="$POSTGRES_USER" \
    -e PGPASSWORD="$POSTGRES_PASSWORD" \
    -e PGDATABASE="$POSTGRES_DB" \
    -e POSTGRES_MIGRATIONS_DIR=/migrations \
    "${migration_mount_args[@]}" \
    "$POSTGRES_MIGRATOR_SERVICE"

echo "==> PostgreSQL initialization complete"
