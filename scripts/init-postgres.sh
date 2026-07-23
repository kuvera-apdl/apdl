#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${POSTGRES_COMPOSE_FILE:-$ROOT_DIR/infra/docker/docker-compose.deps.yml}"
if [[ "$COMPOSE_FILE" != /* ]]; then
    COMPOSE_FILE="$ROOT_DIR/$COMPOSE_FILE"
fi

COMPOSE_ARGS=(-f "$COMPOSE_FILE")
[ -f "$ROOT_DIR/.env" ] && COMPOSE_ARGS=(--env-file "$ROOT_DIR/.env" "${COMPOSE_ARGS[@]}")

env_file_value() {
    local key="$1"
    [ -f "$ROOT_DIR/.env" ] || return 0
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ROOT_DIR/.env"
}
APDL_DEV_API_KEY="${APDL_DEV_API_KEY:-$(env_file_value APDL_DEV_API_KEY)}"
APDL_DEV_CLIENT_KEY="${APDL_DEV_CLIENT_KEY:-$(env_file_value APDL_DEV_CLIENT_KEY)}"

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_MIGRATOR_SERVICE="${POSTGRES_MIGRATOR_SERVICE:-postgres-migrate}"
POSTGRES_USER="${POSTGRES_USER:-apdl}"
POSTGRES_DB="${POSTGRES_DB:-apdl}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(env_file_value POSTGRES_PASSWORD)}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-apdl_dev}"
MIGRATIONS_DIR="${POSTGRES_MIGRATIONS_DIR:-$ROOT_DIR/pipeline/postgres/migrations}"
DEV_CREDENTIAL_SQL="$ROOT_DIR/scripts/provision-dev-credential.sql"
MAINTENANCE_INHIBITOR_LOCK_ID=4158044083
MAINTENANCE_GUARD_LOCK_ID=4158044084

[ -f "$DEV_CREDENTIAL_SQL" ] || {
    echo "Development credential SQL not found: $DEV_CREDENTIAL_SQL" >&2
    exit 1
}

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

# Resolve all image/build work before the final local drain check. The migrator
# then takes the database-authoritative exclusive inhibitor, which closes the
# remaining check/apply race and detects participants outside this Compose project.
docker compose "${COMPOSE_ARGS[@]}" build "$POSTGRES_MIGRATOR_SERVICE" >/dev/null

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

docker compose "${COMPOSE_ARGS[@]}" run --rm --no-deps \
    -e PGHOST="$POSTGRES_SERVICE" \
    -e PGPORT=5432 \
    -e PGUSER="$POSTGRES_USER" \
    -e PGPASSWORD="$POSTGRES_PASSWORD" \
    -e PGDATABASE="$POSTGRES_DB" \
    -e POSTGRES_MIGRATIONS_DIR=/migrations \
    -v "$MIGRATIONS_DIR:/migrations:ro" \
    "$POSTGRES_MIGRATOR_SERVICE"

# Explicit local-development bootstrap. Production deployments should provision
# credentials through their secret-management workflow and leave these unset.
provision_dev_credential() {
    local raw_key="$1"
    local credential_kind="$2"
    local credential_id="$3"
    local roles="$4"
    local project_id
    local key_prefix
    local key_hash

    if [ "$credential_kind" = "confidential" ]; then
        if [[ ! "$raw_key" =~ ^proj_([A-Za-z0-9]{1,64})_([A-Za-z0-9]{16,128})$ ]]; then
            echo "APDL_DEV_API_KEY does not match proj_{project_id}_{secret}" >&2
            exit 1
        fi
        project_id="${BASH_REMATCH[1]}"
        key_prefix="proj_${project_id}_"
    elif [ "$credential_kind" = "browser" ]; then
        if [[ ! "$raw_key" =~ ^client_([A-Za-z0-9]{1,64})_([A-Za-z0-9]{16,128})$ ]]; then
            echo "APDL_DEV_CLIENT_KEY does not match client_{project_id}_{token}" >&2
            exit 1
        fi
        project_id="${BASH_REMATCH[1]}"
        key_prefix="client_${project_id}_"
    else
        echo "Unsupported credential kind: $credential_kind" >&2
        exit 1
    fi

    if command -v sha256sum >/dev/null 2>&1; then
        key_hash="$(printf %s "$raw_key" | sha256sum | awk '{print $1}')"
    else
        key_hash="$(printf %s "$raw_key" | shasum -a 256 | awk '{print $1}')"
    fi
    docker exec -i "$container_id" psql \
        -v ON_ERROR_STOP=1 \
        -v credential_id="$credential_id" \
        -v project_id="$project_id" \
        -v credential_kind="$credential_kind" \
        -v key_prefix="$key_prefix" \
        -v key_hash="$key_hash" \
        -v roles="$roles" \
        -v maintenance_inhibitor_lock_id="$MAINTENANCE_INHIBITOR_LOCK_ID" \
        -v maintenance_guard_lock_id="$MAINTENANCE_GUARD_LOCK_ID" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" >/dev/null < "$DEV_CREDENTIAL_SQL"
    echo "  Provisioned $credential_kind local-development credential for $project_id"
}

if [ -n "${APDL_DEV_API_KEY:-}" ]; then
    provision_dev_credential \
        "$APDL_DEV_API_KEY" \
        "confidential" \
        "local-dev-confidential" \
        "{events:write,config:read,config:write,config:evaluate,query:read}"
fi

if [ -n "${APDL_DEV_CLIENT_KEY:-}" ]; then
    provision_dev_credential \
        "$APDL_DEV_CLIENT_KEY" \
        "browser" \
        "local-dev-browser" \
        "{events:write,config:read}"
fi

echo "==> PostgreSQL initialization complete"
