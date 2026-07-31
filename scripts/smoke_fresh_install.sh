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

# Fixed, public smoke fixtures. Their project is encoded in the canonical key
# prefix; the isolated PostgreSQL instance stores only their hashes.
export APDL_SMOKE_CONFIDENTIAL_KEY="proj_demo_0123456789abcdef0123456789abcdef"
export APDL_SMOKE_BROWSER_KEY="client_demo_0123456789abcdef0123456789abcdef"
export APDL_SERVICE_API_KEYS='{}'
export POSTGRES_PASSWORD="apdl_dev"
export APDL_RUNTIME_POSTGRES_PASSWORD="apdl_runtime_dev"
export APDL_LLM_VAULT_POSTGRES_PASSWORD="apdl_llm_vault_dev"
export APDL_BIND_ADDRESS="127.0.0.1"
# Public, disposable test-only platform keys for the isolated smoke database.
# Compose exposes each value only to its owning controller.
export LLM_VAULT_ENCRYPTION_KEY_BASE64="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
export LLM_VAULT_ADMIN_TOKEN="smoke-llm-vault-admin-token-change-me"
export LLM_VAULT_AGENTS_TOKEN="smoke-llm-vault-agents-token-change-me"
export LLM_VAULT_CODEGEN_TOKEN="smoke-llm-vault-codegen-token-change-me"
export LLM_VAULT_PROJECTION_TOKEN="smoke-llm-vault-projection-token-change-me"
export CODEGEN_CI_POLL_INTERVAL=0
export CODEGEN_STALE_SWEEP_INTERVAL=0

SMOKE_CREDENTIAL_SQL="$ROOT_DIR/scripts/fixtures/provision-smoke-credential.sql"
MAINTENANCE_INHIBITOR_LOCK_ID=4158044083
MAINTENANCE_GUARD_LOCK_ID=4158044084
[ -f "$SMOKE_CREDENTIAL_SQL" ] || {
    echo "Smoke credential fixture not found: $SMOKE_CREDENTIAL_SQL" >&2
    exit 1
}

case "${APDL_SMOKE_ALL_IMAGES:-false}" in
    true)
        smoke_all_images=true
        ;;
    false)
        smoke_all_images=false
        ;;
    *)
        echo "APDL_SMOKE_ALL_IMAGES must be true or false" >&2
        exit 1
        ;;
esac

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

COMPOSE_ARGS=(-f "$COMPOSE_FILE")
if [ -n "${APDL_SMOKE_COMPOSE_OVERRIDE:-}" ]; then
    if [[ "$APDL_SMOKE_COMPOSE_OVERRIDE" != /* ]]; then
        APDL_SMOKE_COMPOSE_OVERRIDE="$ROOT_DIR/$APDL_SMOKE_COMPOSE_OVERRIDE"
    fi
    [ -f "$APDL_SMOKE_COMPOSE_OVERRIDE" ] || {
        echo "Smoke Compose override not found: $APDL_SMOKE_COMPOSE_OVERRIDE" >&2
        exit 1
    }
    COMPOSE_ARGS+=(-f "$APDL_SMOKE_COMPOSE_OVERRIDE")
fi

compose() {
    docker compose "${COMPOSE_ARGS[@]}" "$@"
}

compose_all_profiles() {
    docker compose "${COMPOSE_ARGS[@]}" --profile agents --profile codegen "$@"
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

assert_empty_bootstrap_catalogs() {
    local postgres_id actual expected
    postgres_id="$(compose ps -q postgres)"
    [ -n "$postgres_id" ] || {
        echo "PostgreSQL container is unavailable after initialization" >&2
        return 1
    }

    actual="$(docker exec "$postgres_id" psql -X -A -t \
        -v ON_ERROR_STOP=1 -U apdl -d apdl -c \
        "SELECT catalog || '=' || row_count
         FROM (
             SELECT 'admin_managed_credentials' AS catalog, count(*) AS row_count
             FROM admin_managed_credentials
             UNION ALL
             SELECT 'admin_project_execution_authorizations', count(*)
             FROM admin_project_execution_authorizations
             UNION ALL
             SELECT 'admin_projects', count(*) FROM admin_projects
             UNION ALL
             SELECT 'admin_user_projects', count(*) FROM admin_user_projects
             UNION ALL
             SELECT 'admin_users', count(*) FROM admin_users
             UNION ALL
             SELECT 'auth_credentials', count(*) FROM auth_credentials
             UNION ALL
             SELECT 'llm_project_policies', count(*) FROM llm_project_policies
             UNION ALL
             SELECT 'llm_project_provider_policies', count(*)
             FROM llm_project_provider_policies
         ) AS catalogs
         ORDER BY catalog")"
    expected="admin_managed_credentials=0
admin_project_execution_authorizations=0
admin_projects=0
admin_user_projects=0
admin_users=0
auth_credentials=0
llm_project_policies=0
llm_project_provider_policies=0"
    if [ "$actual" != "$expected" ]; then
        echo "Fresh PostgreSQL bootstrap did not leave identity catalogs empty" >&2
        echo "Expected:" >&2
        printf '%s\n' "$expected" >&2
        echo "Actual:" >&2
        printf '%s\n' "$actual" >&2
        return 1
    fi
    echo "==> Fresh project, user, credential, policy, and authorization catalogs verified empty"
}

provision_smoke_credential() {
    local raw_key="$1"
    local credential_kind="$2"
    local credential_id="$3"
    local roles="$4"
    local project_id
    local key_prefix
    local key_hash

    if [ "$credential_kind" = "confidential" ]; then
        if [[ ! "$raw_key" =~ ^proj_([A-Za-z0-9]{1,64})_([A-Za-z0-9]{16,128})$ ]]; then
            echo "APDL_SMOKE_CONFIDENTIAL_KEY does not match proj_{project_id}_{secret}" >&2
            exit 1
        fi
        project_id="${BASH_REMATCH[1]}"
        key_prefix="proj_${project_id}_"
    elif [ "$credential_kind" = "browser" ]; then
        if [[ ! "$raw_key" =~ ^client_([A-Za-z0-9]{1,64})_([A-Za-z0-9]{16,128})$ ]]; then
            echo "APDL_SMOKE_BROWSER_KEY does not match client_{project_id}_{token}" >&2
            exit 1
        fi
        project_id="${BASH_REMATCH[1]}"
        key_prefix="client_${project_id}_"
    else
        echo "Unsupported smoke credential kind: $credential_kind" >&2
        exit 1
    fi

    if command -v sha256sum >/dev/null 2>&1; then
        key_hash="$(printf %s "$raw_key" | sha256sum | awk '{print $1}')"
    else
        key_hash="$(printf %s "$raw_key" | shasum -a 256 | awk '{print $1}')"
    fi
    docker exec -i "$(compose ps -q postgres)" psql \
        -v ON_ERROR_STOP=1 \
        -v credential_id="$credential_id" \
        -v project_id="$project_id" \
        -v credential_kind="$credential_kind" \
        -v key_prefix="$key_prefix" \
        -v key_hash="$key_hash" \
        -v roles="$roles" \
        -v maintenance_inhibitor_lock_id="$MAINTENANCE_INHIBITOR_LOCK_ID" \
        -v maintenance_guard_lock_id="$MAINTENANCE_GUARD_LOCK_ID" \
        -U apdl \
        -d apdl >/dev/null < "$SMOKE_CREDENTIAL_SQL"
    echo "  Provisioned $credential_kind smoke credential for $project_id"
}

seed_smoke_credentials() {
    provision_smoke_credential \
        "$APDL_SMOKE_CONFIDENTIAL_KEY" \
        "confidential" \
        "smoke-confidential" \
        "{events:write,config:read,config:write,config:evaluate,query:read}"
    provision_smoke_credential \
        "$APDL_SMOKE_BROWSER_KEY" \
        "browser" \
        "smoke-browser" \
        "{events:write,config:read}"
}

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
    expected="smoke-browser|demo|browser|client_demo_|{events:write,config:read}
smoke-confidential|demo|confidential|proj_demo_|{events:write,config:read,config:write,config:evaluate,query:read}"
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
    echo "==> Isolated smoke credentials and operator project verified"
}

assert_operator_recovery_indexes() {
    local postgres_id quarantine_plan
    postgres_id="$(compose ps -q postgres)"
    [ -n "$postgres_id" ] || {
        echo "PostgreSQL container is unavailable after migrations" >&2
        return 1
    }

    quarantine_plan="$(docker exec "$postgres_id" psql -X -A -t \
        -v ON_ERROR_STOP=1 -U apdl -d apdl -c \
        "SET enable_seqscan = off;
         EXPLAIN (COSTS OFF)
         SELECT id
         FROM config_outbox
         WHERE project_id = 'demo'
           AND quarantined_at IS NOT NULL
           AND id < 9223372036854775807
         ORDER BY id DESC
         LIMIT 50;")"
    if ! grep -Fq "idx_config_outbox_quarantine_project_id" \
        <<<"$quarantine_plan"; then
        echo "Project quarantine listing did not use its partial index" >&2
        printf '%s\n' "$quarantine_plan" >&2
        return 1
    fi
    echo "==> Operator recovery indexes verified on PostgreSQL"
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

assert_optional_created() {
    local service container_id
    for service in agents codegen; do
        container_id="$(compose_all_profiles ps -q "$service")"
        if [ -z "$container_id" ]; then
            echo "Published-image smoke did not create optional service: $service" >&2
            return 1
        fi
    done
    echo "==> Optional Agents and Codegen services are healthy"
}

# The writer is the only path from Redis into ClickHouse, and it is the one
# service whose startup authority work can fail after Compose reports the rest
# of the stack healthy. Name that failure directly instead of leaving it to a
# downstream count assertion or a bare Compose exit code.
assert_writer_running() {
    local container_id
    container_id="$(compose ps --status running -q clickhouse-writer || true)"
    if [ -z "$container_id" ]; then
        echo "ClickHouse writer is not running after startup" >&2
        compose ps -a clickhouse-writer >&2 || true
        compose logs --no-color --tail 80 clickhouse-writer >&2 || true
        return 1
    fi
    echo "==> ClickHouse writer is running ($container_id)"
}

assert_writer_consumed() {
    local writer_logs flushed
    assert_writer_running
    writer_logs="$(compose logs --no-color clickhouse-writer || true)"
    flushed="$(grep -cE 'Flushed [1-9][0-9]* events to ClickHouse' \
        <<<"$writer_logs" || true)"
    if [ "${flushed:-0}" -lt 1 ]; then
        echo "ClickHouse writer never flushed a consumed event batch" >&2
        printf '%s\n' "$writer_logs" >&2
        return 1
    fi
    echo "==> ClickHouse writer consumed and flushed $flushed batch(es)"
}

echo "==> Validating core Compose contract"
compose config --quiet

case "${APDL_SMOKE_NO_BUILD:-false}" in
    true)
        [ -n "${APDL_SMOKE_COMPOSE_OVERRIDE:-}" ] || {
            echo "APDL_SMOKE_NO_BUILD=true requires APDL_SMOKE_COMPOSE_OVERRIDE" >&2
            exit 1
        }
        echo "==> Pulling immutable release images without registry credentials"
        published_compose_services=(
            postgres-migrate ingestion config query clickhouse-writer
            llm-vault admin-api admin
        )
        if [ "$smoke_all_images" = true ]; then
            published_compose_services+=(agents codegen)
        fi
        compose_all_profiles pull "${published_compose_services[@]}"
        startup_build_args=(--no-build)
        smoke_migrator_build=false
        smoke_packaged_migrations=true
        ;;
    false)
        if [ "$smoke_all_images" = true ]; then
            echo "APDL_SMOKE_ALL_IMAGES=true requires APDL_SMOKE_NO_BUILD=true" >&2
            exit 1
        fi
        startup_build_args=(--build)
        smoke_migrator_build=true
        smoke_packaged_migrations=false
        ;;
    *)
        echo "APDL_SMOKE_NO_BUILD must be true or false" >&2
        exit 1
        ;;
esac

echo "==> Starting fresh PostgreSQL, ClickHouse, and Redis volumes"
compose up -d postgres clickhouse redis

CLICKHOUSE_COMPOSE_FILE="$COMPOSE_FILE" "$ROOT_DIR/scripts/init-clickhouse.sh"

POSTGRES_MIGRATOR_BUILD="$smoke_migrator_build" \
POSTGRES_USE_PACKAGED_MIGRATIONS="$smoke_packaged_migrations" \
POSTGRES_COMPOSE_FILE="$COMPOSE_FILE" \
POSTGRES_COMPOSE_OVERRIDE_FILE="${APDL_SMOKE_COMPOSE_OVERRIDE:-}" \
    "$ROOT_DIR/scripts/init-postgres.sh"
assert_empty_bootstrap_catalogs
seed_smoke_credentials
assert_credentials
assert_operator_recovery_indexes

echo "==> Proving PostgreSQL runtime/operator privilege separation"
compose run --rm --no-deps \
    --entrypoint bash \
    -e PGHOST=postgres \
    -e PGPORT=5432 \
    -e PGDATABASE=apdl \
    -e APDL_OWNER_POSTGRES_USER=apdl \
    -e APDL_OWNER_POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -e APDL_RUNTIME_POSTGRES_PASSWORD="$APDL_RUNTIME_POSTGRES_PASSWORD" \
    -e APDL_RUNTIME_TEST_POSTGRES_URL=postgresql://apdl_runtime@postgres:5432/apdl \
    -v "$ROOT_DIR/scripts/test_postgres_operator_privileges.sh:/tmp/test_postgres_operator_privileges.sh:ro" \
    postgres-migrate \
    /tmp/test_postgres_operator_privileges.sh

echo "==> Proving real PostgreSQL fence-owner loss rolls back in-flight work"
compose run --rm --no-deps \
    --entrypoint python3 \
    -v "$ROOT_DIR/scripts/test_postgres_fence_owner_loss.py:/tmp/test_postgres_fence_owner_loss.py:ro" \
    postgres-migrate \
    /tmp/test_postgres_fence_owner_loss.py

startup_services=(
    ingestion config query clickhouse-writer admin-api admin gateway
)
if [ "$smoke_all_images" = true ]; then
    startup_services+=(agents codegen)
fi

echo "==> Starting the supported fresh-smoke service set"
compose_all_profiles up -d "${startup_build_args[@]}" --wait \
    --wait-timeout "${APDL_SMOKE_STARTUP_TIMEOUT:-180}" \
    "${startup_services[@]}"
assert_writer_running
if [ "$smoke_all_images" = true ]; then
    assert_optional_created
    [ -n "${APDL_SMOKE_IMAGE_INDEX:-}" ] || {
        echo "APDL_SMOKE_IMAGE_INDEX is required for the all-image smoke" >&2
        exit 1
    }
    "$ROOT_DIR/scripts/smoke_published_auxiliary_images.sh" \
        "$APDL_SMOKE_IMAGE_INDEX"
else
    assert_not_created
fi

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
        --confidential-key "$APDL_SMOKE_CONFIDENTIAL_KEY" \
        --browser-key "$APDL_SMOKE_BROWSER_KEY"
else
    echo "==> Running authoritative experiment-analysis smoke"
    python3 "$ROOT_DIR/scripts/smoke_experiment_analysis.py" \
        --api-key "$APDL_SMOKE_CONFIDENTIAL_KEY" \
        --ingestion-url "$APDL_INGESTION_URL" \
        --config-url "$APDL_CONFIG_URL" \
        --query-url "$APDL_QUERY_URL" \
        --postgres-container-id "$(compose ps -q postgres)"
fi

assert_writer_consumed

echo "==> Fresh-install $SMOKE_SUITE smoke passed"
