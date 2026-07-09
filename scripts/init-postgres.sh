#!/usr/bin/env bash
set -euo pipefail

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

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-apdl}"
POSTGRES_DB="${POSTGRES_DB:-apdl}"
MIGRATIONS_DIR="${POSTGRES_MIGRATIONS_DIR:-$ROOT_DIR/pipeline/postgres/migrations}"

echo "==> Initializing PostgreSQL"
docker compose "${COMPOSE_ARGS[@]}" up -d "$POSTGRES_SERVICE" >/dev/null
container_id="$(docker compose "${COMPOSE_ARGS[@]}" ps -q "$POSTGRES_SERVICE")"
if [ -z "$container_id" ]; then
    echo "PostgreSQL container is not running for compose file: $COMPOSE_FILE" >&2
    exit 1
fi

ready=0
for _ in $(seq 1 30); do
    if docker exec "$container_id" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[ "$ready" -eq 1 ] || { echo "PostgreSQL did not become ready in time." >&2; exit 1; }

for migration in "$MIGRATIONS_DIR"/*.sql; do
    [ -f "$migration" ] || continue
    echo "  Applying $(basename "$migration")"
    docker exec -i "$container_id" psql \
        -v ON_ERROR_STOP=1 \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" < "$migration" >/dev/null
done

# Explicit local-development bootstrap. Production deployments should provision
# credentials through their secret-management workflow and leave this unset.
if [ -n "${APDL_DEV_API_KEY:-}" ]; then
    if [[ ! "$APDL_DEV_API_KEY" =~ ^proj_([A-Za-z0-9]{1,64})_([A-Za-z0-9]{16,128})$ ]]; then
        echo "APDL_DEV_API_KEY does not match proj_{project_id}_{secret}" >&2
        exit 1
    fi
    project_id="${BASH_REMATCH[1]}"
    if command -v sha256sum >/dev/null 2>&1; then
        key_hash="$(printf %s "$APDL_DEV_API_KEY" | sha256sum | awk '{print $1}')"
    else
        key_hash="$(printf %s "$APDL_DEV_API_KEY" | shasum -a 256 | awk '{print $1}')"
    fi
    docker exec -i "$container_id" psql \
        -v ON_ERROR_STOP=1 \
        -v credential_id="local-dev" \
        -v project_id="$project_id" \
        -v key_hash="$key_hash" \
        -U "$POSTGRES_USER" \
        -d "$POSTGRES_DB" >/dev/null <<'SQL'
INSERT INTO auth_credentials (credential_id, project_id, key_hash, roles)
VALUES (
    :'credential_id',
    :'project_id',
    :'key_hash',
    ARRAY[
        'events:write', 'config:read', 'config:write', 'config:evaluate',
        'query:read', 'agents:read', 'agents:run', 'agents:manage',
        'agents:approve'
    ]
)
ON CONFLICT (credential_id) DO UPDATE SET
    project_id = EXCLUDED.project_id,
    key_hash = EXCLUDED.key_hash,
    roles = EXCLUDED.roles,
    active = TRUE,
    expires_at = NULL,
    revoked_at = NULL;
SQL
    echo "  Provisioned explicit local-development credential for $project_id"
fi

echo "==> PostgreSQL initialization complete"
