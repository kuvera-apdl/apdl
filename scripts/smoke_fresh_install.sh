#!/usr/bin/env bash
# Hermetic fresh-install proof for the supported APDL core stack.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/infra/docker/docker-compose.yml"
SMOKE_SUITE="${1:-core}"
case "$SMOKE_SUITE" in
    core|experiment) ;;
    *)
        echo "Usage: $0 [core|experiment]" >&2
        exit 2
        ;;
esac

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Required command not found: $1" >&2
        exit 1
    }
}

require docker
require python3
docker compose version >/dev/null
docker info >/dev/null

# Never share containers, networks, or named volumes with a developer stack or
# another CI job. The migration helpers inherit this exact project identity.
export COMPOSE_PROJECT_NAME="apdl-${SMOKE_SUITE}-fresh-$$-$(date -u +%s)"

# Fixed, public test credentials. Their project is encoded in the canonical key
# prefix; PostgreSQL stores only their hashes.
export APDL_DEV_API_KEY="proj_demo_0123456789abcdef0123456789abcdef"
export APDL_DEV_CLIENT_KEY="client_demo_0123456789abcdef0123456789abcdef"
export APDL_SERVICE_API_KEYS='{}'
export POSTGRES_PASSWORD="apdl_dev"
export APDL_BIND_ADDRESS="127.0.0.1"
export ANTHROPIC_API_KEY=""
export OPENAI_API_KEY=""

# Run beside a normal developer stack. Callers may pin a base or any individual
# port; otherwise the process id gives concurrent local/CI jobs disjoint ranges.
SMOKE_PORT_BASE="${APDL_SMOKE_PORT_BASE:-$((20000 + ($$ % 20000)))}"
if ! [[ "$SMOKE_PORT_BASE" =~ ^[0-9]+$ ]] \
    || [ "$SMOKE_PORT_BASE" -lt 1024 ] \
    || [ "$SMOKE_PORT_BASE" -gt 65525 ]; then
    echo "APDL_SMOKE_PORT_BASE must be an integer from 1024 through 65525" >&2
    exit 1
fi
export APDL_REDIS_HOST_PORT="${APDL_REDIS_HOST_PORT:-$SMOKE_PORT_BASE}"
export APDL_CLICKHOUSE_HTTP_HOST_PORT="${APDL_CLICKHOUSE_HTTP_HOST_PORT:-$((SMOKE_PORT_BASE + 1))}"
export APDL_CLICKHOUSE_NATIVE_HOST_PORT="${APDL_CLICKHOUSE_NATIVE_HOST_PORT:-$((SMOKE_PORT_BASE + 2))}"
export APDL_POSTGRES_HOST_PORT="${APDL_POSTGRES_HOST_PORT:-$((SMOKE_PORT_BASE + 3))}"
export APDL_INGESTION_HOST_PORT="${APDL_INGESTION_HOST_PORT:-$((SMOKE_PORT_BASE + 4))}"
export APDL_CONFIG_HOST_PORT="${APDL_CONFIG_HOST_PORT:-$((SMOKE_PORT_BASE + 5))}"
export APDL_QUERY_HOST_PORT="${APDL_QUERY_HOST_PORT:-$((SMOKE_PORT_BASE + 6))}"
export APDL_GATEWAY_HOST_PORT="${APDL_GATEWAY_HOST_PORT:-$((SMOKE_PORT_BASE + 7))}"
export APDL_AGENTS_HOST_PORT="${APDL_AGENTS_HOST_PORT:-$((SMOKE_PORT_BASE + 8))}"
export APDL_ADMIN_HOST_PORT="${APDL_ADMIN_HOST_PORT:-$((SMOKE_PORT_BASE + 9))}"

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

compose_all_profiles() {
    docker compose -f "$COMPOSE_FILE" --profile agents --profile codegen "$@"
}

cleanup() {
    local status=$? residual_containers residual_networks residual_volumes
    trap - EXIT INT TERM

    if [ "$status" -ne 0 ]; then
        echo "==> Fresh-install smoke failed; capturing Compose state and logs" >&2
        compose_all_profiles ps -a >&2 || true
        compose_all_profiles logs --no-color --timestamps >&2 || true
    fi

    echo "==> Removing isolated Compose project $COMPOSE_PROJECT_NAME"
    if ! compose_all_profiles down -v --remove-orphans --timeout 20; then
        echo "Failed to remove fresh-install containers and volumes" >&2
        [ "$status" -ne 0 ] || status=1
    fi

    residual_containers="$(docker ps -aq \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")"
    residual_networks="$(docker network ls -q \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")"
    residual_volumes="$(docker volume ls -q \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME")"
    if [ -n "$residual_containers$residual_networks$residual_volumes" ]; then
        echo "Fresh-install cleanup left project-labelled Docker resources" >&2
        [ -z "$residual_containers" ] || printf 'Containers: %s\n' "$residual_containers" >&2
        [ -z "$residual_networks" ] || printf 'Networks: %s\n' "$residual_networks" >&2
        [ -z "$residual_volumes" ] || printf 'Volumes: %s\n' "$residual_volumes" >&2
        status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

assert_credentials() {
    local postgres_id actual expected
    postgres_id="$(compose ps -q postgres)"
    [ -n "$postgres_id" ] || {
        echo "PostgreSQL container is unavailable after initialization" >&2
        return 1
    }

    actual="$(docker exec "$postgres_id" psql -X -A -t -F '|' \
        -v ON_ERROR_STOP=1 -U apdl -d apdl -c \
        "SELECT credential_id, project_id, credential_kind, key_prefix, roles::text
         FROM auth_credentials
         WHERE project_id = 'demo'
         ORDER BY credential_id")"
    expected="local-dev-browser|demo|browser|client_demo_|{events:write,config:read}
local-dev-confidential|demo|confidential|proj_demo_|{events:write,config:read,config:write,config:evaluate,query:read}"
    if [ "$actual" != "$expected" ]; then
        echo "Fresh demo credential contract differs" >&2
        echo "Expected:" >&2
        printf '%s\n' "$expected" >&2
        echo "Actual:" >&2
        printf '%s\n' "$actual" >&2
        return 1
    fi

    [ "$(docker exec "$postgres_id" psql -X -A -t -v ON_ERROR_STOP=1 \
        -U apdl -d apdl -c \
        "SELECT COUNT(*) = 1 AND bool_and(created_by IS NULL)
         FROM admin_projects WHERE project_id = 'demo'")" = "t" ] || {
        echo "Fresh demo project is missing or has self-registration provenance" >&2
        return 1
    }
    echo "==> Canonical demo credentials and operator project verified"
}

assert_not_created() {
    local service container_id
    for service in agents codegen; do
        container_id="$(compose_all_profiles ps -a -q "$service" 2>/dev/null || true)"
        if [ -n "$container_id" ]; then
            echo "Unsupported fresh-smoke service was created: $service ($container_id)" >&2
            return 1
        fi
    done
    echo "==> Optional Agents and Codegen services were not created"
}

echo "==> Validating core Compose contract"
compose config --quiet

echo "==> Starting fresh PostgreSQL, ClickHouse, and Redis volumes"
compose up -d postgres clickhouse redis

CLICKHOUSE_COMPOSE_FILE="$COMPOSE_FILE" "$ROOT_DIR/scripts/init-clickhouse.sh"
POSTGRES_COMPOSE_FILE="$COMPOSE_FILE" "$ROOT_DIR/scripts/init-postgres.sh"
assert_credentials

echo "==> Starting only the supported fresh-smoke service set"
compose up -d --build --wait --wait-timeout "${APDL_SMOKE_STARTUP_TIMEOUT:-180}" \
    ingestion config query clickhouse-writer admin-api admin gateway
assert_not_created

export APDL_GATEWAY_URL="http://127.0.0.1:$APDL_GATEWAY_HOST_PORT"
export APDL_INGESTION_URL="http://127.0.0.1:$APDL_INGESTION_HOST_PORT"
export APDL_CONFIG_URL="http://127.0.0.1:$APDL_CONFIG_HOST_PORT"
export APDL_QUERY_URL="http://127.0.0.1:$APDL_QUERY_HOST_PORT"
export APDL_ADMIN_URL="http://127.0.0.1:$APDL_ADMIN_HOST_PORT"

if [ "$SMOKE_SUITE" = "core" ]; then
    echo "==> Running exact-one-event core smoke"
    python3 "$ROOT_DIR/scripts/smoke_core.py" \
        --gateway-url "$APDL_GATEWAY_URL" \
        --ingestion-url "$APDL_INGESTION_URL" \
        --config-url "$APDL_CONFIG_URL" \
        --query-url "$APDL_QUERY_URL" \
        --admin-url "$APDL_ADMIN_URL" \
        --confidential-key "$APDL_DEV_API_KEY" \
        --browser-key "$APDL_DEV_CLIENT_KEY"
else
    echo "==> Running authoritative experiment-analysis smoke"
    python3 "$ROOT_DIR/scripts/smoke_experiment_analysis.py" \
        --api-key "$APDL_DEV_API_KEY" \
        --ingestion-url "$APDL_INGESTION_URL" \
        --config-url "$APDL_CONFIG_URL" \
        --query-url "$APDL_QUERY_URL" \
        --clickhouse-cleanup-url \
            "http://127.0.0.1:$APDL_CLICKHOUSE_HTTP_HOST_PORT" \
        --clickhouse-user apdl \
        --clickhouse-password apdl_dev
fi

echo "==> Fresh-install $SMOKE_SUITE smoke passed"
