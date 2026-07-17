#!/usr/bin/env bash
# APDL master dev script — one entry point for local setup, running, and testing.
#
#   scripts/dev.sh setup      Full local setup (venvs, npm, infra, migrations, .env)
#   scripts/dev.sh up         Start infra deps (Redis, ClickHouse, PostgreSQL) + migrate
#   scripts/dev.sh up-core    Start the supported core stack (default application path)
#   scripts/dev.sh up-full    Opt into core + Agents + offline Codegen
#   scripts/dev.sh smoke-fresh Hermetic fresh-install core proof
#   scripts/dev.sh status     Container status + service health endpoints
#   scripts/dev.sh smoke      End-to-end smoke test against the running stack
#   scripts/dev.sh test       All tests           (make test)
#   scripts/dev.sh lint       All linters         (make lint)
#   scripts/dev.sh check      Parallel lint+test  (make check)
#   scripts/dev.sh logs [svc] Tail Docker logs
#   scripts/dev.sh down       Stop all containers
#   scripts/dev.sh reset      Stop containers and DELETE volumes (asks first)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_VERSION="3.12"
DEPS_COMPOSE="$ROOT_DIR/infra/docker/docker-compose.deps.yml"
FULL_COMPOSE="$ROOT_DIR/infra/docker/docker-compose.yml"
env_file_value() {
    local key="$1"
    [ -f "$ROOT_DIR/.env" ] || return 0
    awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ROOT_DIR/.env"
}
SMOKE_API_KEY="${APDL_DEV_API_KEY:-$(env_file_value APDL_DEV_API_KEY)}"
SMOKE_CLIENT_KEY="${APDL_DEV_CLIENT_KEY:-$(env_file_value APDL_DEV_CLIENT_KEY)}"
INGESTION_HOST_PORT="${APDL_INGESTION_HOST_PORT:-$(env_file_value APDL_INGESTION_HOST_PORT)}"
CONFIG_HOST_PORT="${APDL_CONFIG_HOST_PORT:-$(env_file_value APDL_CONFIG_HOST_PORT)}"
QUERY_HOST_PORT="${APDL_QUERY_HOST_PORT:-$(env_file_value APDL_QUERY_HOST_PORT)}"
AGENTS_HOST_PORT="${APDL_AGENTS_HOST_PORT:-$(env_file_value APDL_AGENTS_HOST_PORT)}"
ADMIN_HOST_PORT="${APDL_ADMIN_HOST_PORT:-$(env_file_value APDL_ADMIN_HOST_PORT)}"
GATEWAY_HOST_PORT="${APDL_GATEWAY_HOST_PORT:-$(env_file_value APDL_GATEWAY_HOST_PORT)}"
INGESTION_HOST_PORT="${INGESTION_HOST_PORT:-8080}"
CONFIG_HOST_PORT="${CONFIG_HOST_PORT:-8081}"
QUERY_HOST_PORT="${QUERY_HOST_PORT:-8082}"
AGENTS_HOST_PORT="${AGENTS_HOST_PORT:-8083}"
ADMIN_HOST_PORT="${ADMIN_HOST_PORT:-5173}"
GATEWAY_HOST_PORT="${GATEWAY_HOST_PORT:-8000}"

# Compose wrappers that load the repo-root .env. With `-f` pointing into
# infra/docker/, Compose's project dir (and its default .env lookup) is that
# folder, so the repo-root .env is otherwise ignored — pass it explicitly.
dc() {
    local file="$1"; shift
    local args=(-f "$file")
    [ -f "$ROOT_DIR/.env" ] && args=(--env-file "$ROOT_DIR/.env" "${args[@]}")
    docker compose "${args[@]}" "$@"
}
dc_full() { dc "$FULL_COMPOSE" "$@"; }
dc_full_all() { dc "$FULL_COMPOSE" --profile agents --profile codegen "$@"; }

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { echo -e "${BLUE}==>${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
die()  { echo -e "${RED}  ✗${NC} $*"; exit 1; }

# ── helpers ──────────────────────────────────────────────────────────

require() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found. $2"
}

# wait_healthy <compose-file> <min-healthy>
wait_healthy() {
    local compose="$1" want="$2" healthy=0
    echo -n "  Waiting for containers to be healthy"
    for _ in $(seq 1 60); do
        healthy=$(dc "$compose" ps --format json 2>/dev/null | grep -c '"healthy"' || true)
        if [ "$healthy" -ge "$want" ] 2>/dev/null; then
            echo ""
            ok "$healthy containers healthy"
            return 0
        fi
        echo -n "."
        sleep 2
    done
    echo ""
    warn "Timed out waiting for health checks ($healthy/$want healthy)"
    return 1
}

# ── setup ────────────────────────────────────────────────────────────

setup_python_package() {
    local name="$1" dir="$2"
    info "Setting up $name"
    [ -d "$dir" ] || { warn "Directory $dir not found, skipping"; return; }
    cd "$dir"
    if [ ! -d ".venv" ]; then
        uv venv --python "$PYTHON_VERSION" .venv
        ok "Created virtualenv"
    else
        ok "Virtualenv already exists"
    fi
    if [ -f "pyproject.toml" ]; then
        uv pip install -e ".[dev]" --python .venv/bin/python --quiet
        ok "Installed dependencies from pyproject.toml"
    elif [ -f "requirements.txt" ]; then
        uv pip install -r requirements.txt --python .venv/bin/python --quiet
        ok "Installed dependencies from requirements.txt"
    fi
    cd "$ROOT_DIR"
}

cmd_setup() {
    info "Checking prerequisites"
    require uv     "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    require node   "Install Node.js >= 20"
    require docker "Install Docker Desktop"
    require curl   "Install curl"
    ok "uv $(uv --version 2>&1 | head -1)"
    ok "node $(node --version)"
    ok "docker $(docker --version | cut -d' ' -f3 | tr -d ',')"

    if [ ! -f "$ROOT_DIR/.env" ] && [ -f "$ROOT_DIR/.env.example" ]; then
        cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
        ok "Created .env from .env.example"
        warn "Review .env; LLM API keys are needed only when opting into Agents"
    else
        ok ".env already exists"
    fi

    setup_python_package "Ingestion Service" "$ROOT_DIR/services/ingestion"
    setup_python_package "Config Service"    "$ROOT_DIR/services/config"
    setup_python_package "Query Service"     "$ROOT_DIR/services/query"
    setup_python_package "Agents Service"    "$ROOT_DIR/services/agents"
    setup_python_package "Codegen Service"   "$ROOT_DIR/services/codegen"
    setup_python_package "Admin API"         "$ROOT_DIR/services/admin-api"
    setup_python_package "Pipeline Writer"   "$ROOT_DIR/pipeline/redis"
    setup_python_package "ETL Framework"     "$ROOT_DIR/pipeline/etl"
    setup_python_package "Python SDK"        "$ROOT_DIR/sdk/python"

    info "Setting up JavaScript SDK"
    (cd "$ROOT_DIR/sdk/javascript" && npm install --silent)
    info "Setting up Admin Console"
    (cd "$ROOT_DIR/services/admin" && npm install --silent)
    ok "Installed npm dependencies"

    cmd_up

    echo ""
    info "Setup complete! Common next steps:"
    echo ""
    echo "  scripts/dev.sh up-core     Run the supported core stack in Docker"
    echo "  scripts/dev.sh up-full     Opt into Agents + offline Codegen too"
    echo "  scripts/dev.sh smoke       End-to-end smoke test"
    echo "  scripts/dev.sh check       Lint + test every package in parallel"
    echo ""
    echo "  make run-ingestion / run-config / run-query / run-agents / run-codegen / run-pipeline"
    echo "                             Run one service locally with hot-reload"
    echo ""
}

# ── stack lifecycle ──────────────────────────────────────────────────

cmd_up() {
    # If an application stack is already running, reuse its compose file so we
    # don't recreate Redis/ClickHouse underneath the application services.
    local compose="$DEPS_COMPOSE"
    if [ -n "$(dc_full ps -q ingestion 2>/dev/null)" ]; then
        compose="$FULL_COMPOSE"
        info "Application stack detected — starting infrastructure via its compose file"
    else
        info "Starting infrastructure (Redis, ClickHouse, PostgreSQL)"
    fi
    dc "$compose" up -d redis clickhouse postgres
    wait_healthy "$compose" 3
    CLICKHOUSE_COMPOSE_FILE="$compose" "$ROOT_DIR/scripts/init-clickhouse.sh"
    ok "ClickHouse schema initialized"
    POSTGRES_COMPOSE_FILE="$compose" "$ROOT_DIR/scripts/init-postgres.sh"
    ok "PostgreSQL schema initialized"
}

cmd_up_full() {
    info "Starting core plus opt-in Agents and offline Codegen (detached)"
    make -C "$ROOT_DIR" --no-print-directory dev-all
    ok "Core and optional application services ready"
    cmd_status
}

cmd_up_core() {
    info "Starting the supported core development stack (detached)"
    make -C "$ROOT_DIR" --no-print-directory dev-core
    ok "Core application services ready"
    cmd_status
}

cmd_down() {
    make -C "$ROOT_DIR" --no-print-directory dev-down
    ok "All containers stopped"
}

cmd_reset() {
    if [ "${1:-}" != "--yes" ]; then
        echo -e "${YELLOW}This deletes all local data volumes (events, flags, agent memory).${NC}"
        read -r -p "Type 'yes' to continue: " answer
        [ "$answer" = "yes" ] || die "Aborted"
    fi
    dc_full_all down -v
    docker compose -f "$DEPS_COMPOSE" down -v
    docker network rm apdl-codegen-development >/dev/null 2>&1 || true
    ok "Containers stopped and volumes removed"
}

cmd_logs() {
    dc_full_all logs -f --tail=100 "$@"
}

# ── status & smoke ───────────────────────────────────────────────────

check_health() {
    local name="$1" url="$2" optional="${3:-}"
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || echo "000")
    if [ "$code" = "200" ]; then
        ok "$name ($url)"
        return 0
    elif [ -n "$optional" ]; then
        warn "$name not responding ($url) — optional, skipping"
        return 1
    else
        echo -e "${RED}  ✗${NC} $name not healthy ($url → $code)"
        return 1
    fi
}

check_compose_health() {
    local name="$1" service="$2" url="$3"
    if dc_full_all exec -T "$service" curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
        ok "$name ($service → $url)"
        return 0
    fi
    echo -e "${RED}  ✗${NC} $name not healthy inside the $service container"
    return 1
}

compose_service_exists() {
    [ -n "$(dc_full_all ps -a -q "$1" 2>/dev/null)" ]
}

compose_service_running() {
    [ -n "$(dc_full_all ps --status running -q "$1" 2>/dev/null)" ]
}

cmd_status() {
    info "Containers"
    dc_full_all ps 2>/dev/null || true
    docker compose -f "$DEPS_COMPOSE" ps 2>/dev/null || true
    echo ""
    info "Service health"
    local failures=0
    check_health "Ingestion" "http://localhost:$INGESTION_HOST_PORT/health" || failures=$((failures+1))
    check_health "Config"    "http://localhost:$CONFIG_HOST_PORT/ready" || failures=$((failures+1))
    check_health "Query"     "http://localhost:$QUERY_HOST_PORT/ready" || failures=$((failures+1))
    check_health "Gateway"   "http://localhost:$GATEWAY_HOST_PORT/" || failures=$((failures+1))
    if compose_service_running clickhouse-writer; then
        ok "ClickHouse writer process running (functional readiness requires scripts/dev.sh smoke)"
    else
        echo -e "${RED}  ✗${NC} ClickHouse writer is not running"
        failures=$((failures+1))
    fi
    if compose_service_exists agents; then
        check_health "Agents" "http://localhost:$AGENTS_HOST_PORT/ready" || failures=$((failures+1))
    else
        info "Agents disabled (opt in with scripts/dev.sh up-full or make dev-all)"
    fi
    if compose_service_exists codegen; then
        check_compose_health "Codegen" "codegen" "http://127.0.0.1:8084/ready" || failures=$((failures+1))
    else
        info "Codegen disabled (opt in with scripts/dev.sh up-full or make dev-all; publication stays offline)"
    fi
    if compose_service_exists admin || compose_service_exists admin-api; then
        check_health "Admin API" "http://localhost:$ADMIN_HOST_PORT/api/ready" || failures=$((failures+1))
    else
        info "Admin console not started"
    fi
    [ "$failures" -eq 0 ] || warn "$failures required/enabled service(s) unhealthy — are they running? (scripts/dev.sh up-core)"
    [ "$failures" -eq 0 ]
}

cmd_smoke() {
    [ -n "$SMOKE_API_KEY" ] || die "APDL_DEV_API_KEY is required for the smoke test"
    [ -n "$SMOKE_CLIENT_KEY" ] || die "APDL_DEV_CLIENT_KEY is required for the smoke test"
    require python3 "Install Python 3.12"
    info "Running the canonical core smoke against the current stack"
    APDL_DEV_API_KEY="$SMOKE_API_KEY" \
    APDL_DEV_CLIENT_KEY="$SMOKE_CLIENT_KEY" \
    APDL_GATEWAY_URL="http://localhost:$GATEWAY_HOST_PORT" \
    APDL_INGESTION_URL="http://localhost:$INGESTION_HOST_PORT" \
    APDL_CONFIG_URL="http://localhost:$CONFIG_HOST_PORT" \
    APDL_QUERY_URL="http://localhost:$QUERY_HOST_PORT" \
    APDL_ADMIN_URL="http://localhost:$ADMIN_HOST_PORT" \
        python3 "$ROOT_DIR/scripts/smoke_core.py"
    ok "Core smoke passed"
}

cmd_smoke_fresh() {
    make -C "$ROOT_DIR" --no-print-directory smoke-fresh
}

# ── dispatch ─────────────────────────────────────────────────────────

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-help}" in
    setup)   cmd_setup ;;
    up)      cmd_up ;;
    up-core) cmd_up_core ;;
    up-full) cmd_up_full ;;
    down)    cmd_down ;;
    reset)   shift; cmd_reset "$@" ;;
    status)  cmd_status ;;
    smoke)   cmd_smoke ;;
    smoke-fresh) cmd_smoke_fresh ;;
    test)    make -C "$ROOT_DIR" test ;;
    lint)    make -C "$ROOT_DIR" lint ;;
    check)   make -C "$ROOT_DIR" check ;;
    fmt)     make -C "$ROOT_DIR" fmt ;;
    logs)    shift; cmd_logs "$@" ;;
    help|-h|--help) usage ;;
    *)       usage; die "Unknown command: $1" ;;
esac
